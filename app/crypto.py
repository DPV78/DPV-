"""Criptografia das credenciais do PJe (Fernet: AES-128-CBC + HMAC-SHA256).

A chave fica fora do banco (variável CREDENTIALS_KEY). Gere uma com:  python -m app.crypto
"""
from cryptography.fernet import Fernet, InvalidToken

from .config import settings


class CryptoUnavailable(Exception):
    pass


def _fernet() -> Fernet:
    if not settings.credentials_key:
        raise CryptoUnavailable("CREDENTIALS_KEY não configurada: não é possível guardar credenciais do PJe.")
    try:
        return Fernet(settings.credentials_key.encode())
    except (ValueError, TypeError) as e:
        raise CryptoUnavailable("CREDENTIALS_KEY inválida (gere com: python -m app.crypto).") from e


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise CryptoUnavailable("Não foi possível decifrar a credencial (a CREDENTIALS_KEY mudou?).") from e


if __name__ == "__main__":
    print(Fernet.generate_key().decode())
