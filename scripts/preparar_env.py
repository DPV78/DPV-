"""Prepara o arquivo .env para o teste local: gera as chaves e cria o usuário administrador.

Executado pelo iniciar-teste.bat. Nunca imprime senhas nem chaves.
"""
import getpass
import re
import secrets
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def get(text: str, key: str) -> str:
    m = re.search(rf"^{key}=(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def put(text: str, key: str, value: str) -> str:
    if re.search(rf"^{key}=.*$", text, re.M):
        return re.sub(rf"^{key}=.*$", lambda _: f"{key}={value}", text, flags=re.M)
    return text.rstrip("\n") + f"\n{key}={value}\n"


def _has_users(db: Path) -> bool:
    import sqlite3

    try:
        with sqlite3.connect(db) as con:
            return con.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
    except sqlite3.Error:
        return False


def main() -> int:
    if not ENV.exists():
        shutil.copy(EXAMPLE, ENV)
        print("[ok] Arquivo .env criado a partir do modelo.")
    text = read(ENV)

    if get(text, "SECRET_KEY") in ("", "troque-esta-chave", "dev-inseguro-troque"):
        text = put(text, "SECRET_KEY", secrets.token_urlsafe(48))
        print("[ok] Chave de sessão gerada.")
    if not get(text, "CREDENTIALS_KEY"):
        from cryptography.fernet import Fernet

        text = put(text, "CREDENTIALS_KEY", Fernet.generate_key().decode())
        print("[ok] Chave de criptografia das credenciais do PJe gerada.")
        print("     Guarde uma cópia do arquivo .env em local seguro (sem ela, as senhas salvas não podem ser lidas).")

    if not get(text, "ADMIN_EMAIL") or not get(text, "ADMIN_PASSWORD"):
        db = ROOT / "carteira.db"
        if db.exists() and _has_users(db):
            print("[ok] Banco já existe com usuários: use o login que você já criou.")
        else:
            print()
            print("Crie o usuário administrador do aplicativo (não é a senha do PJe).")
            email = input("  E-mail: ").strip().lower()
            while True:
                pw = getpass.getpass("  Senha (mín. 10 caracteres; não aparece ao digitar): ")
                if len(pw) < 10:
                    print("  A senha precisa ter ao menos 10 caracteres.")
                    continue
                if getpass.getpass("  Repita a senha: ") != pw:
                    print("  As senhas não conferem.")
                    continue
                break
            text = put(text, "ADMIN_EMAIL", email)
            text = put(text, "ADMIN_PASSWORD", pw)
            print("[ok] Administrador configurado.")

    ENV.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(1)
