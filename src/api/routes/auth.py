"""Simple API key authentication via header.

Validates the X-API-Key header against the configured api_secret_key.
"""

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from src.config import Settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_settings() -> Settings:
    """Return application settings (overridden in tests via dependency override)."""
    return Settings()


async def verify_api_key(
    api_key: str | None = Security(_api_key_header),
    settings: Settings = Depends(get_settings),
) -> str:
    """Validate the API key from the request header.

    In development mode, allows requests without an API key.
    Returns the API key on success, raises 401 on failure.
    """
    # In development, allow unauthenticated access
    if not settings.is_production:
        return api_key or "dev"

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide X-API-Key header.",
        )
    if api_key != settings.api_secret_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )
    return api_key
