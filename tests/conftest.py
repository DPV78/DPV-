import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["SYNC_INTERVAL_HOURS"] = "0"
os.environ["ADMIN_EMAIL"] = "admin@escritorio.test"
os.environ["ADMIN_PASSWORD"] = "senha-segura-123"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["DATAJUD_API_KEY"] = "chave-teste"
os.environ["CREDENTIALS_KEY"] = "q8iZ4R2xG3b7qkq0bKf2y8k7cO8m3yQ2mS0p5r6t7u8="
os.environ["DATA_DIR"] = f"{_tmp}/dados"
os.environ["AVISOS_INTERVAL_HOURS"] = "0"
