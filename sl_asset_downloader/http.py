"""Small HTTP helper with retries, built on urllib (honours HTTPS_PROXY)."""

from __future__ import annotations

import logging
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

from . import __version__, llsd

log = logging.getLogger(__name__)

USER_AGENT = f"SLAssetDownloader/{__version__}"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: bytes = b""):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} for {url.split('?')[0]}")


class HttpClient:
    def __init__(self, ca_bundle: str | None = None, insecure: bool = False,
                 timeout: float = 60.0, retries: int = 4):
        if insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx = ssl.create_default_context(cafile=ca_bundle)
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx))
        self.timeout = timeout
        self.retries = retries

    def request(self, method: str, url: str, body: bytes | None = None,
                headers: dict[str, str] | None = None) -> tuple[bytes, dict[str, str]]:
        hdrs = {"User-Agent": USER_AGENT}
        if headers:
            hdrs.update(headers)
        delay = 1.0
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    return resp.read(), dict(resp.headers.items())
            except urllib.error.HTTPError as e:
                payload = e.read() if e.fp else b""
                if e.code in RETRY_STATUSES and attempt < self.retries:
                    wait = delay
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    if retry_after and retry_after.isdigit():
                        wait = max(wait, float(retry_after))
                    log.debug("HTTP %s on %s, retrying in %.1fs", e.code, url.split("?")[0], wait)
                    time.sleep(wait)
                    delay = min(delay * 2, 30)
                    continue
                raise HttpError(e.code, url, payload) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.retries:
                    log.debug("network error %s on %s, retrying in %.1fs", e, url.split("?")[0], delay)
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise
        raise AssertionError("unreachable")

    def get(self, url: str) -> bytes:
        return self.request("GET", url)[0]

    def post_llsd(self, url: str, value: Any) -> Any:
        data, _ = self.request(
            "POST", url, llsd.format_xml(value),
            {"Content-Type": llsd.CONTENT_TYPE, "Accept": llsd.CONTENT_TYPE})
        return llsd.parse_xml(data) if data.strip() else None
