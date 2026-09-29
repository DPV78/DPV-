"""Backup do banco SQLite (cópia consistente, mesmo com o aplicativo em uso).

Uso manual:  python -m app.backup
O aplicativo também executa um backup diário automaticamente (23h) e mantém os últimos BACKUP_KEEP arquivos.
Copie periodicamente a pasta DATA_DIR (backups + documentos) para outro disco ou nuvem do escritório.
"""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import settings

KEEP = int(os.getenv("BACKUP_KEEP", "30"))


def run_backup() -> Path | None:
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        return None  # PostgreSQL: use pg_dump
    src = Path(url.removeprefix("sqlite:///"))
    dest_dir = Path(settings.data_dir) / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"carteira_{datetime.now():%Y%m%d_%H%M%S}.db"
    with sqlite3.connect(src) as s, sqlite3.connect(dest) as d:
        s.backup(d)
    for old in sorted(dest_dir.glob("carteira_*.db"))[:-KEEP]:
        old.unlink()
    return dest


if __name__ == "__main__":
    print(run_backup() or "Banco não é SQLite; use pg_dump.")
