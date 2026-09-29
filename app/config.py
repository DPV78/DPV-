"""Configuração lida de variáveis de ambiente (ou de um arquivo .env)."""
import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class Settings:
    secret_key: str = os.getenv("SECRET_KEY", "dev-inseguro-troque")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./carteira.db")

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
    claude_effort: str = os.getenv("CLAUDE_EFFORT", "high")

    datajud_api_key: str = os.getenv("DATAJUD_API_KEY", "")
    datajud_base_url: str = os.getenv("DATAJUD_BASE_URL", "https://api-publica.datajud.cnj.jus.br")

    djen_base_url: str = os.getenv("DJEN_BASE_URL", "https://comunicaapi.pje.jus.br/api/v1")
    djen_oabs: list[str] = [o.strip() for o in os.getenv("DJEN_OABS", "").split(",") if o.strip()]
    djen_date_format: str = os.getenv("DJEN_DATE_FORMAT", "iso")

    sync_interval_hours: float = float(os.getenv("SYNC_INTERVAL_HOURS", "6"))
    avisos_interval_hours: float = float(os.getenv("AVISOS_INTERVAL_HOURS", "2"))

    # Chave Fernet para criptografar as credenciais do PJe (gere com: python -m app.crypto)
    credentials_key: str = os.getenv("CREDENTIALS_KEY", "")
    # Em rede local sem HTTPS, deixe false; com HTTPS (proxy reverso), use true
    session_https_only: bool = os.getenv("SESSION_HTTPS_ONLY", "false").lower() == "true"
    # Pasta para documentos baixados do PJe e backups
    data_dir: str = os.getenv("DATA_DIR", "./dados")

    admin_email: str = os.getenv("ADMIN_EMAIL", "")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    admin_name: str = os.getenv("ADMIN_NAME", "Administrador")


settings = Settings()
