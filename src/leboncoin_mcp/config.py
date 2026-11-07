"""Environment-driven settings."""

from __future__ import annotations

import os
from dataclasses import dataclass

LOCAL_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")


def parse_hosts(value: str | None) -> tuple[str, ...]:
    """Comma-separated `Host` header values; `name:*` accepts any port."""
    extra = tuple(h.strip() for h in (value or "").split(",") if h.strip())
    return LOCAL_HOSTS + extra


@dataclass(frozen=True)
class Settings:
    cdp_url: str = "http://127.0.0.1:9222"
    novnc_url: str = "http://localhost:6080/vnc.html?autoconnect=1&resize=scale"
    host: str = "127.0.0.1"
    port: int = 8000
    transport: str = "stdio"
    allowed_hosts: tuple[str, ...] = LOCAL_HOSTS
    lock_timeout_s: float = 30.0
    operation_timeout_s: float = 60.0
    navigation_timeout_ms: int = 30_000

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        return cls(
            cdp_url=os.getenv("LEBONCOIN_MCP_CDP_URL", defaults.cdp_url),
            novnc_url=os.getenv("LEBONCOIN_MCP_NOVNC_URL", defaults.novnc_url),
            host=os.getenv("LEBONCOIN_MCP_HOST", defaults.host),
            port=int(os.getenv("LEBONCOIN_MCP_PORT", defaults.port)),
            transport=os.getenv("LEBONCOIN_MCP_TRANSPORT", defaults.transport),
            allowed_hosts=parse_hosts(os.getenv("LEBONCOIN_MCP_ALLOWED_HOSTS")),
        )
