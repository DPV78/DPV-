"""Modelo de dados da carteira estratégica."""
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now()


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


class Case(Base):
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
    objetivo: Mapped[str | None] = mapped_column(Text)  # resultado que o cliente precisa
    estrategia: Mapped[str | None] = mapped_column(Text)  # tese central e caminho processual
    riscos: Mapped[str | None] = mapped_column(Text)
    premissas: Mapped[str | None] = mapped_column(Text)  # fatos/condições que, se mudarem, exigem revisão

    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_sync_status: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    responsavel: Mapped[User | None] = relationship()
    movements: Mapped[list["Movement"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="desc(Movement.data)"
    )
    steps: Mapped[list["NextStep"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    decisions: Mapped[list["Decision"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="desc(Decision.created_at)"
    )
    strategy_versions: Mapped[list["StrategyVersion"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="desc(StrategyVersion.created_at)"
    )
    notes: Mapped[list["Note"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="desc(Note.created_at)"
    )


class Movement(Base):
    """Andamento: manual, DataJud (movimentos) ou DJEN (publicações/intimações)."""

    __tablename__ = "movements"
    __table_args__ = (UniqueConstraint("case_id", "fonte", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    data: Mapped[datetime] = mapped_column(DateTime, index=True)
    fonte: Mapped[str] = mapped_column(String(20))  # manual / datajud / djen
    external_id: Mapped[str] = mapped_column(String(200))
    titulo: Mapped[str] = mapped_column(String(500))
    texto: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(500))
    lido: Mapped[bool] = mapped_column(Boolean, default=False)
    analise: Mapped[str | None] = mapped_column(Text)  # triagem do assistente
    impacto: Mapped[str | None] = mapped_column(String(10))  # alto / medio / baixo
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    case: Mapped[Case] = relationship(back_populates="movements")


class NextStep(Base):
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

    case: Mapped[Case] = relationship(back_populates="steps")
    responsavel: Mapped[User | None] = relationship(foreign_keys=[responsavel_id])


class Decision(Base):
    """Ponto de decisão: o que fazer diante de um andamento. Discutido com o assistente e entre advogados."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    titulo: Mapped[str] = mapped_column(String(300))
    contexto: Mapped[str | None] = mapped_column(Text)
    movement_id: Mapped[int | None] = mapped_column(ForeignKey("movements.id"))
    status: Mapped[str] = mapped_column(String(20), default="aberta")  # aberta / decidida
    decisao: Mapped[str | None] = mapped_column(Text)  # o que foi decidido
    fundamentos: Mapped[str | None] = mapped_column(Text)  # por quê
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    sugestoes_json: Mapped[str | None] = mapped_column(Text)  # próximos passos sugeridos pelo assistente
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    case: Mapped[Case] = relationship(back_populates="decisions")
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
    """Histórico da estratégia: cada revisão fica registrada com autor e motivo."""

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


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    texto: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    case: Mapped[Case] = relationship(back_populates="notes")
    author: Mapped[User | None] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), index=True)
    acao: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    user: Mapped[User | None] = relationship()
