"""Sincronização de andamentos: DataJud (movimentos) + DJEN (publicações)."""
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .cnj import digits
from .config import settings
from .integrations import datajud, djen
from . import audit, pje_service
from .models import Case, Movement

log = logging.getLogger("sync")


def _upsert(db: Session, case: Case, fonte: str, external_id: str, data: datetime, titulo: str,
            texto: str | None, link: str | None = None) -> bool:
    exists = db.scalar(
        select(Movement.id).where(
            Movement.case_id == case.id, Movement.fonte == fonte, Movement.external_id == external_id
        )
    )
    if exists:
        return False
    db.add(Movement(case_id=case.id, fonte=fonte, external_id=external_id, data=data,
                    titulo=titulo[:500], texto=texto, link=link, lido=False))
    return True


def sync_case(db: Session, case: Case, *, datajud_client=None, djen_client=None) -> dict:
    """Busca novidades de um processo. Retorna contagem de novos andamentos por fonte e erros."""
    novos = {"pje": 0, "datajud": 0, "djen": 0}
    erros = []

    try:
        novos["pje"], erros_pje = pje_service.sync_case_movements(db, case)
        erros += erros_pje
    except Exception as e:  # falha no PJe não impede as demais fontes
        log.exception("Falha no MNI para %s", case.numero_cnj)
        erros.append(f"PJe: {type(e).__name__}")

    if settings.datajud_api_key:
        try:
            res = datajud.fetch(case.numero_cnj, case.tribunal, client=datajud_client)
            if res.found:
                case.classe = case.classe or res.classe
                case.orgao_julgador = case.orgao_julgador or res.orgao_julgador
                for m in res.movements:
                    if _upsert(db, case, "datajud", m.external_id, m.data, m.titulo, m.texto):
                        novos["datajud"] += 1
            else:
                erros.append("DataJud: processo não localizado (confira o tribunal ou aguarde a carga)")
        except datajud.DataJudError as e:
            erros.append(str(e))
    else:
        erros.append("DataJud não configurado")

    try:
        inicio = (case.last_sync_at.date() - timedelta(days=7)) if case.last_sync_at else date.today() - timedelta(days=365)
        for item in djen.fetch(numero_processo=case.numero_cnj, inicio=inicio, fim=date.today(), client=djen_client):
            if _upsert(db, case, "djen", item.external_id, item.data, item.titulo, item.texto, item.link):
                novos["djen"] += 1
    except djen.DjenError as e:
        erros.append(str(e))

    case.last_sync_at = datetime.now()
    total = sum(novos.values())
    case.last_sync_status = (f"{total} novo(s)" + (" · " + " | ".join(erros) if erros else ""))[:300]
    audit.log(db, f"Sincronização: {novos['pje']} PJe, {novos['datajud']} DataJud, {novos['djen']} DJEN"
              + (f" — avisos: {' | '.join(erros)}" if erros else ""), tipo="sincronizar", case_id=case.id)
    db.commit()
    return {"novos": novos, "erros": erros}


def sync_oab_publications(db: Session, *, days: int = 3, client=None) -> dict:
    """Varre o DJEN pelas OABs configuradas e vincula publicações aos processos da carteira."""
    if not settings.djen_oabs:
        return {"vinculadas": 0, "fora_da_carteira": []}
    cases = {digits(c.numero_cnj): c for c in db.scalars(select(Case).where(Case.deleted_at.is_(None))).all()}
    vinculadas, fora = 0, set()
    for oab in settings.djen_oabs:
        items = djen.fetch(oab=oab, inicio=date.today() - timedelta(days=days), fim=date.today(), client=client)
        for it in items:
            case = cases.get(it.numero_processo)
            if case is None:
                fora.add(it.numero_processo)
                continue
            if _upsert(db, case, "djen", it.external_id, it.data, it.titulo, it.texto, it.link):
                vinculadas += 1
    db.commit()
    return {"vinculadas": vinculadas, "fora_da_carteira": sorted(fora)}


def sync_all(session_factory) -> None:
    db = session_factory()
    try:
        for case in db.scalars(select(Case).where(Case.status == "ativo", Case.deleted_at.is_(None))).all():
            try:
                sync_case(db, case)
            except Exception:  # um processo com erro não interrompe os demais
                log.exception("Falha ao sincronizar %s", case.numero_cnj)
                db.rollback()
        try:
            sync_oab_publications(db)
        except Exception:
            log.exception("Falha na varredura por OAB")
            db.rollback()
    finally:
        db.close()


def check_avisos_job(session_factory) -> None:
    db = session_factory()
    try:
        pje_service.check_avisos(db)
    except Exception:
        log.exception("Falha na consulta de intimações do PJe")
        db.rollback()
    finally:
        db.close()
