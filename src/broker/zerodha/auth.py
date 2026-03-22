"""Zerodha Kite Connect authentication and token management."""

import logging
from pathlib import Path

from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)

TOKEN_FILE = Path(".kite_access_token")


class KiteAuth:
    """Handles Kite Connect login flow and token persistence."""

    def __init__(self, api_key: str, api_secret: str):
        self._api_key = api_key
        self._api_secret = api_secret
        self._kite = KiteConnect(api_key=api_key)

    @property
    def login_url(self) -> str:
        """Get the Kite login URL for browser-based authentication."""
        return self._kite.login_url()

    def generate_session(self, request_token: str) -> str:
        """Exchange request token for access token.

        Args:
            request_token: The request token received after user login.

        Returns:
            The access token string.
        """
        data = self._kite.generate_session(
            request_token=request_token, api_secret=self._api_secret
        )
        access_token = data["access_token"]
        self._save_token(access_token)
        logger.info("Access token generated and saved")
        return access_token

    def get_saved_token(self) -> str | None:
        """Load previously saved access token."""
        if TOKEN_FILE.exists():
            token = TOKEN_FILE.read_text().strip()
            if token:
                return token
        return None

    def _save_token(self, access_token: str) -> None:
        """Persist access token to file and update .env."""
        TOKEN_FILE.write_text(access_token)
        # Restrict file permissions
        TOKEN_FILE.chmod(0o600)
        # Also update .env so the server picks up the new token
        self._update_env_token(access_token)

    def _update_env_token(self, access_token: str) -> None:
        """Update KITE_ACCESS_TOKEN in .env file."""
        env_path = Path(".env")
        if not env_path.exists():
            return
        lines = env_path.read_text().splitlines()
        updated = False
        for i, line in enumerate(lines):
            if line.startswith("KITE_ACCESS_TOKEN="):
                lines[i] = f"KITE_ACCESS_TOKEN={access_token}"
                updated = True
                break
        if updated:
            env_path.write_text("\n".join(lines) + "\n")
            logger.info("Updated KITE_ACCESS_TOKEN in .env")
