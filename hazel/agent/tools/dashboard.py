"""Dashboard link tool — generates time-limited authenticated URLs for the Canvas dashboard."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path
from typing import Any


from hazel.agent.tools.base import Tool

SECRET_PATH = Path.home() / ".hazel" / "dashboard.key"


def get_or_create_secret() -> str:
    """Read the shared HMAC secret, creating it if it doesn't exist."""
    if SECRET_PATH.exists():
        return SECRET_PATH.read_text(encoding="utf-8").strip()
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    SECRET_PATH.write_text(secret, encoding="utf-8")
    SECRET_PATH.chmod(0o600)
    return secret


def generate_token(secret: str, ttl_seconds: int) -> str:
    """Create an HMAC-SHA256 token: ``{expiry_hex}.{signature_hex}``."""
    expiry = int(time.time()) + ttl_seconds
    expiry_hex = format(expiry, "x")
    sig = hmac.new(
        secret.encode("utf-8"), expiry_hex.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{expiry_hex}.{sig}"


class DashboardLinkTool(Tool):
    """Generate a time-limited secure link to the Hazel Canvas dashboard."""

    def __init__(
        self,
        *,
        dashboard_host: str = "127.0.0.1",
        dashboard_port: int = 8081,
        dashboard_base_url: str = "",
        token_ttl_minutes: int = 60,
    ):
        self._host = dashboard_host
        self._port = dashboard_port
        self._base_url = dashboard_base_url
        self._ttl_minutes = token_ttl_minutes

    @property
    def name(self) -> str:
        return "dashboard_link"

    @property
    def description(self) -> str:
        return (
            "Generate a time-limited secure URL for the Hazel Canvas dashboard. "
            "The link expires after the configured TTL (default 60 minutes). "
            "Send the resulting URL to the user through the chat channel."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ttl_minutes": {
                    "type": "integer",
                    "description": (
                        "How long the link should be valid, in minutes. "
                        f"Default: {self._ttl_minutes}."
                    ),
                },
            },
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        ttl = kwargs.get("ttl_minutes", self._ttl_minutes)
        if ttl < 1:
            return "Error: ttl_minutes must be at least 1."
        if ttl > 1440:
            return "Error: ttl_minutes must be at most 1440 (24 hours)."

        secret = get_or_create_secret()
        token = generate_token(secret, ttl * 60)

        if self._base_url:
            base = self._base_url.rstrip("/")
        else:
            base = f"http://{self._host}:{self._port}"

        url = f"{base}/?token={token}"

        return (
            f"Dashboard link (valid for {ttl} minutes):\n{url}\n\n"
            "Send this link to the user. When it expires, they can ask you for a new one."
        )
