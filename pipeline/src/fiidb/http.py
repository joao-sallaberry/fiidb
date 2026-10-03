import hashlib
import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; fiidb/0.1)"


def client(timeout: float = 120) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)


def fetch(
    http: httpx.Client,
    url: str,
    *,
    params: dict | None = None,
    cache_dir: Path | None = None,
    retries: int = 3,
) -> bytes | None:
    """GET a URL. Returns None on 404. When cache_dir is given, the body is cached on disk
    (only use it for files that never change, like closed-year archives)."""
    cached = cache_dir / url.rsplit("/", 1)[-1] if cache_dir else None
    if cached and cached.exists():
        return cached.read_bytes()

    for attempt in range(1, retries + 1):
        try:
            resp = http.get(url, params=params)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            break
        except httpx.HTTPError as exc:
            if attempt == retries:
                raise
            log.warning("GET %s failed (%s), retrying", url, exc)
            time.sleep(2 * attempt)

    if cached:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(resp.content)
    return resp.content


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
