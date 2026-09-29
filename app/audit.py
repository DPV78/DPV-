"""Auditoria automática de todas as alterações.

Um listener do SQLAlchemy (`before_flush`) examina cada objeto criado, alterado ou apagado e grava em
`audit_log` quem fez, quando, de qual IP e o que mudou (valores antes/depois). Assim nenhuma rota precisa
lembrar de registrar: qualquer escrita no banco feita pelo aplicativo fica registrada, inclusive as
feitas pela sincronização automática (usuário "Sistema").

Os registros são encadeados por hash (SHA-256): alterar ou apagar uma linha diretamente no banco quebra a
cadeia, o que é detectado por `verify_chain`.
"""
import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from .models import (
    AuditLog, Case, Decision, DiscussionMessage, Movement, NextStep, Note, PjeAviso, PjeCredential,
    PjeDocument, PjeEndpoint, StrategyVersion, User,
)


@dataclass
class Actor:
    user_id: int | None = None
    user_name: str | None = None
    ip: str | None = None


current_actor: ContextVar[Actor] = ContextVar("current_actor", default=Actor(user_name="Sistema"))

ENTITY_NAMES = {
    Case: "processo", Movement: "andamento", NextStep: "proximo_passo", Decision: "decisao",
    DiscussionMessage: "mensagem", StrategyVersion: "estratégia", Note: "nota", User: "usuario",
    PjeEndpoint: "pje_endpoint", PjeCredential: "pje_credencial", PjeAviso: "pje_intimacao",
    PjeDocument: "pje_documento",
}
# Campos nunca gravados na auditoria (apenas indicamos que mudaram)
SECRET_FIELDS = {"password_hash", "cpf_enc", "senha_enc"}
# Campos técnicos cuja alteração isolada não interessa ao administrador
NOISE_FIELDS = {"updated_at", "last_sync_at", "last_sync_status", "last_ok_at", "last_seen_at", "sugestoes_json"}
LABELS = {
    "processo": lambda o: f"{o.numero_cnj} — {o.titulo}",
    "andamento": lambda o: o.titulo,
    "proximo_passo": lambda o: o.descricao,
    "decisao": lambda o: o.titulo,
    "nota": lambda o: (o.texto or "")[:80],
    "estratégia": lambda o: f"versão — {o.motivo or ''}",
    "mensagem": lambda o: (o.content or "")[:80],
    "usuario": lambda o: o.email,
    "pje_endpoint": lambda o: f"{o.tribunal} {o.instancia}",
    "pje_intimacao": lambda o: f"{o.numero_processo} aviso {o.id_aviso}",
    "pje_documento": lambda o: o.descricao,
}


def _json(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _snapshot(obj) -> dict:
    data = {}
    for col in inspect(obj).mapper.column_attrs:
        k = col.key
        data[k] = "***" if k in SECRET_FIELDS else _json(getattr(obj, k))
    return data


def _case_id(obj) -> int | None:
    if isinstance(obj, Case):
        return obj.id
    if isinstance(obj, DiscussionMessage):
        dec = obj.decision
        return dec.case_id if dec is not None else None
    return getattr(obj, "case_id", None)


def _label(entity: str, obj) -> str:
    try:
        return str(LABELS.get(entity, lambda o: "")(obj))[:150]
    except Exception:
        return ""


def _row_hash(entry: AuditLog, prev: str) -> str:
    payload = json.dumps(
        [prev, entry.created_at.isoformat(), entry.user_id, entry.ip, entry.case_id, entry.entidade,
         entry.entidade_id, entry.tipo, entry.acao, entry.detalhes],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _last_hash(session: Session) -> str:
    pending = [o for o in session.new if isinstance(o, AuditLog) and o.hash]
    if pending:
        return pending[-1].hash
    with session.no_autoflush:
        return session.scalar(select(AuditLog.hash).where(AuditLog.hash.is_not(None)).order_by(AuditLog.id.desc()).limit(1)) or ""


def log(session: Session, acao: str, *, tipo: str = "acao", case_id: int | None = None, entidade: str | None = None,
        entidade_id: int | None = None, detalhes: dict | None = None) -> AuditLog:
    """Registro explícito (login, consulta ao PJe, abertura de intimação, exportação…)."""
    actor = current_actor.get()
    entry = AuditLog(
        created_at=datetime.now(), user_id=actor.user_id, user_name=actor.user_name, ip=actor.ip,
        case_id=case_id, entidade=entidade, entidade_id=entidade_id, tipo=tipo, acao=acao[:300],
        detalhes=json.dumps(detalhes, ensure_ascii=False, default=str) if detalhes else None,
    )
    entry.prev_hash = _last_hash(session)
    entry.hash = _row_hash(entry, entry.prev_hash)
    session.add(entry)
    return entry


@event.listens_for(Session, "before_flush")
def _audit_changes(session: Session, flush_context, instances) -> None:
    # Criações são registradas depois do INSERT, quando o id já existe (ver _audit_creations)
    pending = session.info.setdefault("audit_new", [])
    pending.extend(o for o in session.new if type(o) in ENTITY_NAMES)

    for obj in list(session.dirty):
        entity = ENTITY_NAMES.get(type(obj))
        if entity is None or not session.is_modified(obj, include_collections=False):
            continue
        with session.no_autoflush:
            case_id = _case_id(obj)
        state = inspect(obj)
        antes, depois = {}, {}
        for attr in state.mapper.column_attrs:
            hist = state.attrs[attr.key].history
            if not hist.has_changes():
                continue
            old = hist.deleted[0] if hist.deleted else None
            new = hist.added[0] if hist.added else None
            if old == new:
                continue
            if attr.key in SECRET_FIELDS:
                antes[attr.key], depois[attr.key] = "***", "*** (alterado)"
            else:
                antes[attr.key], depois[attr.key] = _json(old), _json(new)
        if not antes or set(antes) <= NOISE_FIELDS:
            continue
        tipo = "editar"
        if "deleted_at" in antes:
            tipo = "excluir" if depois["deleted_at"] else "restaurar"
        verbo = {"editar": "Editou", "excluir": "Excluiu (lixeira)", "restaurar": "Restaurou"}[tipo]
        log(session, f"{verbo} {entity.replace('_', ' ')}: {_label(entity, obj)}", tipo=tipo,
            case_id=case_id, entidade=entity, entidade_id=obj.id, detalhes={"antes": antes, "depois": depois})

    for obj in list(session.deleted):
        entity = ENTITY_NAMES.get(type(obj))
        if entity is None:
            continue
        with session.no_autoflush:
            case_id = _case_id(obj)
        log(session, f"Expurgou definitivamente {entity.replace('_', ' ')}: {_label(entity, obj)}", tipo="expurgar",
            case_id=case_id, entidade=entity, entidade_id=obj.id, detalhes={"antes": _snapshot(obj)})


@event.listens_for(Session, "after_flush_postexec")
def _audit_creations(session: Session, flush_context) -> None:
    pending = session.info.pop("audit_new", [])
    for obj in pending:
        entity = ENTITY_NAMES[type(obj)]
        snap = {k: v for k, v in _snapshot(obj).items() if v not in (None, "", False)}
        with session.no_autoflush:
            case_id = _case_id(obj)
        log(session, f"Criou {entity.replace('_', ' ')}: {_label(entity, obj)}", tipo="criar",
            case_id=case_id, entidade=entity, entidade_id=obj.id, detalhes={"depois": snap})


@event.listens_for(Session, "after_rollback")
def _discard_pending(session: Session) -> None:
    session.info.pop("audit_new", None)


def verify_chain(session: Session) -> tuple[bool, int | None, int]:
    """Recalcula a cadeia. Retorna (íntegra?, id do primeiro registro inconsistente, total verificado)."""
    prev = ""
    total = 0
    for entry in session.scalars(select(AuditLog).where(AuditLog.hash.is_not(None)).order_by(AuditLog.id)):
        total += 1
        if entry.prev_hash != prev or _row_hash(entry, prev) != entry.hash:
            return False, entry.id, total
        prev = entry.hash
    return True, None, total
