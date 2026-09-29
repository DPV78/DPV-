"""Criação e atualização automática do banco.

`create_all` cria tabelas novas; para tabelas já existentes, adicionamos as colunas que faltarem
(ALTER TABLE ... ADD COLUMN). Assim, atualizar o aplicativo não apaga dados. Não removemos colunas.
"""
import logging

from sqlalchemy import func, inspect, select, text

from .auth import hash_password
from .config import settings
from .db import Base, SessionLocal, engine
from .models import User

log = logging.getLogger("migrate")


def add_missing_columns() -> list[str]:
    insp = inspect(engine)
    added = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl_type = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}'))
                added.append(f"{table.name}.{col.name}")
    if added:
        log.warning("Colunas adicionadas ao banco: %s", ", ".join(added))
    return added


def init_db() -> None:
    Base.metadata.create_all(engine)
    add_missing_columns()
    from .pje_presets import seed_presets

    with SessionLocal() as db:
        from .models import PjeEndpoint

        if not db.scalar(select(func.count(PjeEndpoint.id))):
            seed_presets(db)  # primeira execução: TJRO e TRF1 (1º e 2º graus)
            db.commit()
    if settings.admin_email and settings.admin_password:
        with SessionLocal() as db:
            if not db.scalar(select(func.count(User.id))):
                db.add(User(name=settings.admin_name, email=settings.admin_email.lower(),
                            password_hash=hash_password(settings.admin_password), is_admin=True))
                db.commit()
