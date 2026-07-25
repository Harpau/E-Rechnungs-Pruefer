from __future__ import annotations

import urllib.error
import urllib.request

DIRECT_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def health_is_ready(port: int, timeout: float = 0.5) -> bool:
    """Probe the public loopback health endpoint without server dependencies."""

    try:
        with DIRECT_HTTP_OPENER.open(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False
