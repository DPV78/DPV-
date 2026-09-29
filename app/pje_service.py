"""Regras de negócio da integração autenticada com o PJe (MNI)."""
import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, crypto
from .cnj import digits, format_cnj
from .config import settings
from .integrations import mni
from .integrations.mni import Credential, Documento, MniClient, MniError
from .models import Case, Movement, PjeAviso, PjeCredential, PjeDocument, PjeEndpoint, User

log = logging.getLogger("pje")

EXT = {"application/pdf": ".pdf", "text/html": ".html", "text/plain": ".txt", "image/jpeg": ".jpg", "image/png": ".png",
       "application/msword": ".doc", "video/mp4": ".mp4", "audio/mpeg": ".mp3"}


class PjeUnavailable(Exception):
    pass


def client_for(endpoint: PjeEndpoint, http=None) -> MniClient:
    return MniClient(endpoint.url, endpoint.versao_mni, client=http)


def credential_of(cred: PjeCredential) -> Credential:
    return Credential(cpf=crypto.decrypt(cred.cpf_enc), senha=crypto.decrypt(cred.senha_enc))


def endpoints_for(db: Session, tribunal: str) -> list[PjeEndpoint]:
    return list(db.scalars(select(PjeEndpoint).where(PjeEndpoint.tribunal == tribunal.lower(), PjeEndpoint.ativo.is_(True))))


def pick_credential(db: Session, endpoint: PjeEndpoint, prefer_user_id: int | None) -> PjeCredential | None:
    creds = list(db.scalars(select(PjeCredential).where(PjeCredential.endpoint_id == endpoint.id, PjeCredential.ativo.is_(True))))
    creds.sort(key=lambda c: (c.user_id != prefer_user_id, c.last_error is not None))
    return creds[0] if creds else None


def _mark(cred: PjeCredential, error: MniError | None) -> None:
    if error is not None and error.kind == "nao_encontrado":
        return  # processo inexistente naquela instância não indica problema na credencial
    if error is None:
        cred.last_ok_at, cred.last_error = datetime.now(), None
    else:
        cred.last_error = f"{error.kind}: {error}"[:500]
        if error.kind == "credencial":
            cred.ativo = False  # evita bloqueio da conta por tentativas repetidas com senha errada


def sync_case_movements(db: Session, case: Case, http=None) -> tuple[int, list[str]]:
    """Consulta o processo no MNI de cada instância cadastrada para o tribunal. Retorna (novos, avisos)."""
    novos, erros, nao_encontrado, achou = 0, [], [], False
    eps = endpoints_for(db, case.tribunal)
    if not eps:
        return 0, []
    for ep in eps:
        cred = pick_credential(db, ep, case.responsavel_id)
        if cred is None:
            erros.append(f"PJe {ep.tribunal.upper()} {ep.instancia}: nenhuma credencial ativa")
            continue
        try:
            proc = client_for(ep, http).consultar_processo(credential_of(cred), case.numero_cnj)
            _mark(cred, None)
        except MniError as e:
            _mark(cred, e)
            if e.kind == "nao_encontrado":
                nao_encontrado.append(f"PJe {ep.tribunal.upper()} {ep.instancia}: processo não encontrado nesta instância")
                continue
            erros.append(f"PJe {ep.tribunal.upper()} {ep.instancia}: {e}")
            audit.log(db, f"Consulta ao PJe falhou ({ep.tribunal} {ep.instancia}): {e}", tipo="pje", case_id=case.id)
            continue
        except crypto.CryptoUnavailable as e:
            erros.append(str(e))
            continue
        achou = True
        case.orgao_julgador = case.orgao_julgador or proc.orgao
        for m in proc.movimentos:
            ext = f"{ep.instancia}|{m.external_id}"[:200]
            exists = db.scalar(select(Movement.id).where(Movement.case_id == case.id, Movement.fonte == "pje", Movement.external_id == ext))
            if exists:
                continue
            titulo = f"[PJe {ep.instancia}] {m.descricao}"
            db.add(Movement(case_id=case.id, fonte="pje", external_id=ext, data=m.data, titulo=titulo[:500], lido=False))
            novos += 1
        audit.log(db, f"Consultou o processo no PJe ({ep.tribunal} {ep.instancia}) com a credencial de {cred.user.name}: {len(proc.movimentos)} movimento(s)",
                  tipo="pje", case_id=case.id)
    # É normal o processo existir só em um grau; só avisamos se não foi encontrado em nenhum
    if not achou and nao_encontrado:
        erros += nao_encontrado
    return novos, erros


def diagnose_endpoint(db: Session, ep: PjeEndpoint, http=None) -> "mni.Diagnostico":
    """Testa o endereço (WSDL, sem credenciais) e, se identificar a versão do MNI, atualiza o cadastro."""
    diag = mni.diagnosticar(ep.url, client=http)
    ep.last_check_at, ep.last_check_ok, ep.last_check_status = datetime.now(), diag.ok, diag.mensagem[:500]
    if diag.versao in {"2.2.2", "2.2.3"} and diag.versao != ep.versao_mni:
        ep.versao_mni = diag.versao
    audit.log(db, f"Diagnóstico do MNI {ep.tribunal.upper()} {ep.instancia}: {diag.mensagem}", tipo="pje")
    return diag


def check_avisos(db: Session, http=None, only_user_id: int | None = None) -> dict:
    """Consulta intimações pendentes de cada credencial ativa. NÃO registra ciência."""
    q = select(PjeCredential).where(PjeCredential.ativo.is_(True))
    if only_user_id:
        q = q.where(PjeCredential.user_id == only_user_id)
    cases = {digits(c.numero_cnj): c for c in db.scalars(select(Case).where(Case.deleted_at.is_(None)))}
    novos, erros = 0, []
    for cred in db.scalars(q).all():
        ep = cred.endpoint
        if not ep.ativo:
            continue
        try:
            avisos = client_for(ep, http).consultar_avisos_pendentes(credential_of(cred))
            _mark(cred, None)
        except (MniError, crypto.CryptoUnavailable) as e:
            if isinstance(e, MniError):
                _mark(cred, e)
            erros.append(f"{cred.user.name} / {ep.tribunal.upper()} {ep.instancia}: {e}")
            continue
        vistos = set()
        for a in avisos:
            vistos.add(a.id_aviso)
            row = db.scalar(select(PjeAviso).where(PjeAviso.endpoint_id == ep.id, PjeAviso.id_aviso == a.id_aviso))
            case = cases.get(a.numero_processo)
            if row is None:
                db.add(PjeAviso(
                    endpoint_id=ep.id, credential_id=cred.id, case_id=case.id if case else None, id_aviso=a.id_aviso,
                    numero_processo=format_cnj(a.numero_processo), tipo_comunicacao=a.tipo_comunicacao, orgao=a.orgao,
                    destinatario=a.destinatario, data_disponibilizacao=a.data_disponibilizacao,
                ))
                novos += 1
            else:
                row.last_seen_at = datetime.now()
                if row.case_id is None and case:
                    row.case_id = case.id
        # Pendentes desta credencial que sumiram da lista foram abertas por outro meio (ex.: PJe web) ou expiraram
        for row in db.scalars(select(PjeAviso).where(PjeAviso.credential_id == cred.id, PjeAviso.status == "pendente")):
            if row.id_aviso not in vistos:
                row.status = "fora_do_pje"
        audit.log(db, f"Consultou intimações pendentes no PJe ({ep.tribunal} {ep.instancia}) de {cred.user.name}: {len(avisos)}", tipo="pje")
    db.commit()
    return {"novos": novos, "erros": erros}


def _save_document(case_id: int | None, doc: Documento) -> tuple[str, str]:
    content = doc.conteudo or b""
    sha = hashlib.sha256(content).hexdigest()
    folder = Path(settings.data_dir) / "documentos" / str(case_id or "sem-processo")
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-. ]", "_", doc.descricao)[:80].strip() or "documento"
    path = folder / f"{sha[:12]}_{safe}{EXT.get(doc.mimetype, '')}"
    path.write_bytes(content)
    return str(path), sha


def open_teor(db: Session, aviso: PjeAviso, user: User, http=None) -> PjeAviso:
    """Abre o teor da intimação com a credencial DO PRÓPRIO USUÁRIO. REGISTRA A CIÊNCIA NO PJe."""
    if aviso.status == "aberto":
        return aviso
    cred = db.scalar(select(PjeCredential).where(PjeCredential.user_id == user.id, PjeCredential.endpoint_id == aviso.endpoint_id,
                                                 PjeCredential.ativo.is_(True)))
    if cred is None:
        raise PjeUnavailable("Você não tem credencial ativa do PJe para este tribunal/instância. A ciência é registrada em nome de quem abre.")
    audit.log(db, f"Solicitou abertura do teor da intimação {aviso.id_aviso} ({aviso.numero_processo}) — registra ciência no PJe",
              tipo="pje_ciencia", case_id=aviso.case_id, entidade="pje_intimacao", entidade_id=aviso.id)
    db.commit()
    try:
        teor = client_for(aviso.endpoint, http).consultar_teor_comunicacao(credential_of(cred), aviso.numero_processo, aviso.id_aviso)
        _mark(cred, None)
    except MniError as e:
        _mark(cred, e)
        audit.log(db, f"Falha ao abrir o teor da intimação {aviso.id_aviso}: {e}", tipo="pje_ciencia", case_id=aviso.case_id)
        db.commit()
        raise PjeUnavailable(f"O tribunal não devolveu o teor: {e}. Verifique no PJe se a ciência foi registrada.") from e
    aviso.status, aviso.teor = "aberto", teor.texto
    aviso.prazo_dias, aviso.tipo_prazo = teor.prazo_dias, teor.tipo_prazo
    aviso.opened_by_id, aviso.opened_at = user.id, datetime.now()
    for doc in teor.documentos:
        if doc.conteudo:
            path, sha = _save_document(aviso.case_id, doc)
            db.add(PjeDocument(case_id=aviso.case_id, aviso_id=aviso.id, id_documento=doc.id_documento, descricao=doc.descricao,
                               mimetype=doc.mimetype, path=path, sha256=sha, downloaded_by_id=user.id))
    if aviso.case_id:
        prazo = f" Prazo informado pelo tribunal: {teor.prazo_dias} dia(s){f' ({teor.tipo_prazo})' if teor.tipo_prazo else ''}." if teor.prazo_dias else ""
        db.add(Movement(
            case_id=aviso.case_id, fonte="pje-intimacao", external_id=f"{aviso.endpoint_id}|{aviso.id_aviso}",
            data=datetime.combine(aviso.data_disponibilizacao, datetime.min.time()) if aviso.data_disponibilizacao else datetime.now(),
            titulo=f"Intimação PJe ({aviso.tipo_comunicacao or 'comunicação'}) — ciência registrada em {datetime.now():%d/%m/%Y %H:%M} por {user.name}.{prazo}"[:500],
            texto=teor.texto, lido=False, created_by_id=user.id,
        ))
    audit.log(db, f"Teor da intimação {aviso.id_aviso} aberto — CIÊNCIA REGISTRADA NO PJe em nome de {user.name}",
              tipo="pje_ciencia", case_id=aviso.case_id, entidade="pje_intimacao", entidade_id=aviso.id,
              detalhes={"prazo_dias": teor.prazo_dias, "tipo_prazo": teor.tipo_prazo, "documentos": len(teor.documentos)})
    db.commit()
    return aviso


def download_document(db: Session, case: Case, id_documento: str, user: User, http=None) -> PjeDocument:
    for ep in endpoints_for(db, case.tribunal):
        cred = db.scalar(select(PjeCredential).where(PjeCredential.user_id == user.id, PjeCredential.endpoint_id == ep.id,
                                                     PjeCredential.ativo.is_(True))) or pick_credential(db, ep, user.id)
        if cred is None:
            continue
        try:
            proc = client_for(ep, http).consultar_processo(credential_of(cred), case.numero_cnj, incluir_documentos=True,
                                                           documentos=[id_documento])
        except MniError as e:
            _mark(cred, e)
            continue
        doc = next((d for d in proc.documentos if d.id_documento == id_documento and d.conteudo), None)
        if doc is None:
            continue
        path, sha = _save_document(case.id, doc)
        row = PjeDocument(case_id=case.id, id_documento=id_documento, descricao=doc.descricao, mimetype=doc.mimetype,
                          path=path, sha256=sha, downloaded_by_id=user.id)
        db.add(row)
        audit.log(db, f"Baixou do PJe o documento {id_documento} ({doc.descricao}) com a credencial de {cred.user.name}",
                  tipo="pje", case_id=case.id)
        db.commit()
        return row
    raise PjeUnavailable("Documento não obtido: sem credencial ativa ou o tribunal não devolveu o conteúdo.")


def list_documents(db: Session, case: Case, user: User, http=None) -> list[Documento]:
    """Lista as peças do processo em todas as instâncias (metadados). O conteúdo que vier junto é descartado."""
    docs, falhas = [], []
    for ep in endpoints_for(db, case.tribunal):
        cred = pick_credential(db, ep, user.id)
        if cred is None:
            continue
        try:
            proc = client_for(ep, http).consultar_processo(credential_of(cred), case.numero_cnj, incluir_documentos=True)
        except MniError as e:
            _mark(cred, e)
            falhas.append(f"{ep.instancia}: {e}")
            continue
        audit.log(db, f"Listou as peças do processo no PJe ({ep.tribunal} {ep.instancia})", tipo="pje", case_id=case.id)
        for d in proc.documentos:
            d.conteudo = None
            d.instancia = ep.instancia
        docs.extend(proc.documentos)
    db.commit()
    if docs:
        return docs
    raise PjeUnavailable(" | ".join(falhas) or "Nenhum endereço MNI com credencial ativa para este tribunal.")
