"""Modelo de dados da carteira estratégica.

Exclusões são reversíveis ("lixeira"): os registros recebem `deleted_at`/`deleted_by_id` e somem das telas,
mas continuam no banco até que um administrador os expurgue. Toda alteração é registrada em `audit_log`
automaticamente (ver app/audit.py).
"""
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now()


class SoftDelete:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    deleted_by_id: Mapped[int | None] = mapped_column(Integer)

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    oab: Mapped[str | None] = mapped_column(String(30))
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Case(SoftDelete, Base):
    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    numero_cnj: Mapped[str] = mapped_column(String(25), unique=True, index=True)
    tribunal: Mapped[str] = mapped_column(String(20))  # alias DataJud, ex.: tjsp, trf1, stj
    titulo: Mapped[str] = mapped_column(String(250))
    cliente: Mapped[str] = mapped_column(String(250))
    polo: Mapped[str] = mapped_column(String(20), default="ativo")  # ativo / passivo / terceiro
    parte_contraria: Mapped[str | None] = mapped_column(String(250))
    orgao_julgador: Mapped[str | None] = mapped_column(String(250))
    classe: Mapped[str | None] = mapped_column(String(250))
    valor_causa: Mapped[str | None] = mapped_column(String(60))
    fase: Mapped[str | None] = mapped_column(String(120))
    prioridade: Mapped[str] = mapped_column(String(10), default="alta")  # critica / alta / media
    status: Mapped[str] = mapped_column(String(20), default="ativo")  # ativo / suspenso / encerrado
    responsavel_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    # Estratégia de longo prazo: guia o passo a passo
    objetivo: Mapped[str | None] = mapped_column(Text)
    estrategia: Mapped[str | None] = mapped_column(Text)
    riscos: Mapped[str | None] = mapped_column(Text)
    premissas: Mapped[str | None] = mapped_column(Text)

    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_sync_status: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    responsavel: Mapped[User | None] = relationship()
    all_movements: Mapped[list["Movement"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="desc(Movement.data)"
    )
    all_steps: Mapped[list["NextStep"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    all_decisions: Mapped[list["Decision"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="desc(Decision.created_at)"
    )
    strategy_versions: Mapped[list["StrategyVersion"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="desc(StrategyVersion.created_at)"
    )
    all_notes: Mapped[list["Note"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="desc(Note.created_at)"
    )

    # Visões sem os itens da lixeira — é o que as telas e o assistente usam
    @property
    def movements(self) -> list["Movement"]:
        return [m for m in self.all_movements if m.deleted_at is None]

    @property
    def steps(self) -> list["NextStep"]:
        return [s for s in self.all_steps if s.deleted_at is None]

    @property
    def decisions(self) -> list["Decision"]:
        return [d for d in self.all_decisions if d.deleted_at is None]

    @property
    def notes(self) -> list["Note"]:
        return [n for n in self.all_notes if n.deleted_at is None]


class Movement(SoftDelete, Base):
    """Andamento: manual, DataJud, DJEN, PJe (MNI) ou intimação do PJe."""

    __tablename__ = "movements"
    __table_args__ = (UniqueConstraint("case_id", "fonte", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    data: Mapped[datetime] = mapped_column(DateTime, index=True)
    fonte: Mapped[str] = mapped_column(String(20))  # manual / datajud / djen / pje / pje-intimacao
    external_id: Mapped[str] = mapped_column(String(200))
    titulo: Mapped[str] = mapped_column(String(500))
    texto: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(500))
    lido: Mapped[bool] = mapped_column(Boolean, default=False)
    analise: Mapped[str | None] = mapped_column(Text)  # triagem do assistente
    impacto: Mapped[str | None] = mapped_column(String(10))  # alto / medio / baixo
    editado: Mapped[bool] = mapped_column(Boolean, default=False)  # alterado manualmente após importação
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    case: Mapped[Case] = relationship(back_populates="all_movements")


class NextStep(SoftDelete, Base):
    __tablename__ = "next_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    descricao: Mapped[str] = mapped_column(Text)
    prazo: Mapped[date | None] = mapped_column(Date)
    prazo_fatal: Mapped[bool] = mapped_column(Boolean, default=False)
    responsavel_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="pendente")  # pendente / concluido / cancelado
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)

    case: Mapped[Case] = relationship(back_populates="all_steps")
    responsavel: Mapped[User | None] = relationship(foreign_keys=[responsavel_id])


class Decision(SoftDelete, Base):
    """Ponto de decisão: o que fazer diante de um andamento. Discutido com o assistente e entre advogados."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    titulo: Mapped[str] = mapped_column(String(300))
    contexto: Mapped[str | None] = mapped_column(Text)
    movement_id: Mapped[int | None] = mapped_column(ForeignKey("movements.id"))
    status: Mapped[str] = mapped_column(String(20), default="aberta")  # aberta / decidida
    decisao: Mapped[str | None] = mapped_column(Text)
    fundamentos: Mapped[str | None] = mapped_column(Text)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    sugestoes_json: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    case: Mapped[Case] = relationship(back_populates="all_decisions")
    movement: Mapped[Movement | None] = relationship()
    decided_by: Mapped[User | None] = relationship(foreign_keys=[decided_by_id])
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    messages: Mapped[list["DiscussionMessage"]] = relationship(
        back_populates="decision", cascade="all, delete-orphan", order_by="DiscussionMessage.id"
    )


class DiscussionMessage(Base):
    __tablename__ = "discussion_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # user / assistant
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    decision: Mapped[Decision] = relationship(back_populates="messages")
    author: Mapped[User | None] = relationship()


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    objetivo: Mapped[str | None] = mapped_column(Text)
    estrategia: Mapped[str | None] = mapped_column(Text)
    riscos: Mapped[str | None] = mapped_column(Text)
    premissas: Mapped[str | None] = mapped_column(Text)
    motivo: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    case: Mapped[Case] = relationship(back_populates="strategy_versions")
    author: Mapped[User | None] = relationship()


class Note(SoftDelete, Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    texto: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    case: Mapped[Case] = relationship(back_populates="all_notes")
    author: Mapped[User | None] = relationship()


# ------------------------------------------------------------------ PJe (MNI)

class PjeEndpoint(Base):
    """Endereço do webservice MNI de um tribunal/instância (cadastrado pelo administrador)."""

    __tablename__ = "pje_endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    tribunal: Mapped[str] = mapped_column(String(20), index=True)  # mesmo alias usado no processo (tjmg, trt3…)
    instancia: Mapped[str] = mapped_column(String(20), default="1g")  # 1g / 2g / turma recursal…
    url: Mapped[str] = mapped_column(String(500))  # ex.: https://pje.tjmg.jus.br/pje/intercomunicacao
    versao_mni: Mapped[str] = mapped_column(String(10), default="2.2.2")
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    origem: Mapped[str | None] = mapped_column(String(300))  # de onde veio o endereço (confirmado/inferido)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_check_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_check_status: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PjeCredential(Base):
    """CPF e senha do PJe de um advogado para um endpoint. Armazenados criptografados (app/crypto.py)."""

    __tablename__ = "pje_credentials"
    __table_args__ = (UniqueConstraint("user_id", "endpoint_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("pje_endpoints.id"), index=True)
    cpf_enc: Mapped[str] = mapped_column(Text)
    senha_enc: Mapped[str] = mapped_column(Text)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    user: Mapped[User] = relationship()
    endpoint: Mapped[PjeEndpoint] = relationship()


class PjeAviso(Base):
    """Intimação/citação pendente no PJe (consultarAvisosPendentes).

    Ler o teor (consultarTeorComunicacao) REGISTRA A CIÊNCIA no PJe e pode iniciar o prazo —
    só acontece por ação expressa e confirmada de um advogado.
    """

    __tablename__ = "pje_avisos"
    __table_args__ = (UniqueConstraint("endpoint_id", "id_aviso"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("pje_endpoints.id"), index=True)
    credential_id: Mapped[int | None] = mapped_column(ForeignKey("pje_credentials.id"))
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), index=True)
    id_aviso: Mapped[str] = mapped_column(String(100))
    numero_processo: Mapped[str] = mapped_column(String(25), index=True)
    tipo_comunicacao: Mapped[str | None] = mapped_column(String(60))
    orgao: Mapped[str | None] = mapped_column(String(300))
    destinatario: Mapped[str | None] = mapped_column(String(300))
    data_disponibilizacao: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="pendente")  # pendente / aberto / fora_do_pje
    teor: Mapped[str | None] = mapped_column(Text)
    prazo_dias: Mapped[int | None] = mapped_column(Integer)
    tipo_prazo: Mapped[str | None] = mapped_column(String(30))
    opened_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    endpoint: Mapped[PjeEndpoint] = relationship()
    case: Mapped[Case | None] = relationship()
    opened_by: Mapped[User | None] = relationship(foreign_keys=[opened_by_id])
    documents: Mapped[list["PjeDocument"]] = relationship(back_populates="aviso")


class PjeDocument(Base):
    """Documento obtido do PJe (anexo de intimação ou peça do processo), salvo em DATA_DIR/documentos."""

    __tablename__ = "pje_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), index=True)
    aviso_id: Mapped[int | None] = mapped_column(ForeignKey("pje_avisos.id"))
    id_documento: Mapped[str | None] = mapped_column(String(100))
    descricao: Mapped[str] = mapped_column(String(300))
    mimetype: Mapped[str] = mapped_column(String(100), default="application/pdf")
    path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    downloaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    aviso: Mapped[PjeAviso | None] = relationship(back_populates="documents")


# ------------------------------------------------------------------ auditoria

class AuditLog(Base):
    """Trilha de auditoria. Somente inclusão: não há tela nem rota para alterar ou apagar registros.

    `hash` encadeia cada registro ao anterior (SHA-256), permitindo detectar alteração direta no banco.
    `case_id` não é chave estrangeira de propósito: o registro sobrevive ao expurgo do processo.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    user_name: Mapped[str | None] = mapped_column(String(120))
    ip: Mapped[str | None] = mapped_column(String(64))
    case_id: Mapped[int | None] = mapped_column(Integer, index=True)
    entidade: Mapped[str | None] = mapped_column(String(40), index=True)
    entidade_id: Mapped[int | None] = mapped_column(Integer)
    tipo: Mapped[str | None] = mapped_column(String(30), index=True)  # criar/editar/excluir/restaurar/expurgar/acesso/pje…
    acao: Mapped[str] = mapped_column(String(300))
    detalhes: Mapped[str | None] = mapped_column(Text)  # JSON: antes/depois
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    hash: Mapped[str | None] = mapped_column(String(64))
