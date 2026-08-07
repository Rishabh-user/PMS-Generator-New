"""Encryption at rest for saved AI-provider API keys (see
app.services.ai_provider_store / the /admin/ai-settings page).

Uses Fernet (symmetric, authenticated) keyed by
AI_CREDENTIALS_ENCRYPTION_KEY. There is no silent fallback when that
setting is unset — better to fail loudly at the point a key would be
encrypted/decrypted than to store something with a guessable or
missing key.
"""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class CredentialCryptoError(RuntimeError):
    pass


@lru_cache
def _fernet() -> Fernet:
    key = (settings.ai_credentials_encryption_key or "").strip()
    if not key:
        raise CredentialCryptoError(
            "AI_CREDENTIALS_ENCRYPTION_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            "and add it to .env before saving or reading AI provider credentials."
        )
    try:
        return Fernet(key.encode())
    except ValueError as e:
        raise CredentialCryptoError(
            f"AI_CREDENTIALS_ENCRYPTION_KEY is not a valid Fernet key: {e}"
        ) from e


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise CredentialCryptoError(
            "Stored credential could not be decrypted — "
            "AI_CREDENTIALS_ENCRYPTION_KEY may have changed since it was saved."
        ) from e
