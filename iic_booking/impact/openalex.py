"""One way to call the OpenAlex API: adds the institute's API key (OPENALEX_API_KEY) and contact address
(OPENALEX_MAILTO) when set, and waits and retries when OpenAlex throttles (HTTP 429) or briefly fails (5xx).

Without a key, OpenAlex shares a small free daily budget among everyone on the server's IP address. The key is
sent as a query parameter, as OpenAlex requires; it is never logged (errors carry only the status and reason).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

HOST = "api.openalex.org"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
MAX_WAIT_SECONDS = 30.0


class OpenAlexError(Exception):
    """An OpenAlex request failed; the message never contains the URL or the API key."""

    def __init__(self, status: int | None, reason: str):
        self.status = status
        super().__init__(f"OpenAlex request failed ({status or 'network'}: {reason})")


def with_credentials(url: str) -> str:
    """Add api_key / mailto to an OpenAlex URL (only for the OpenAlex API host; other URLs are returned unchanged)."""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname != HOST:
        return url
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    key = (getattr(settings, "OPENALEX_API_KEY", "") or "").strip()
    mailto = (getattr(settings, "OPENALEX_MAILTO", "") or "").strip()
    if key:
        query["api_key"] = key
    if mailto and "mailto" not in query:
        query["mailto"] = mailto
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _wait_seconds(exc: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = (exc.headers or {}).get("Retry-After") if exc.headers is not None else None
    try:
        wait = float(retry_after) if retry_after else 0.0
    except ValueError:
        wait = 0.0
    return min(MAX_WAIT_SECONDS, wait or 2.0 * (2 ** attempt))


def get_json(url: str, *, user_agent: str, timeout: float = 20, sleep=time.sleep) -> dict:
    """GET an OpenAlex URL and return its JSON body, retrying throttled or failed requests a few times."""
    req_url = with_credentials(url)
    last: OpenAlexError | None = None
    for attempt in range(MAX_ATTEMPTS):
        req = urllib.request.Request(req_url, headers={"User-Agent": user_agent})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = OpenAlexError(exc.code, str(exc.reason))
            if exc.code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS - 1:
                break
            wait = _wait_seconds(exc, attempt)
            logger.info("OpenAlex HTTP %s; retrying in %.0fs", exc.code, wait)
            sleep(wait)
        except (urllib.error.URLError, TimeoutError) as exc:
            last = OpenAlexError(None, type(exc).__name__)
            if attempt == MAX_ATTEMPTS - 1:
                break
            sleep(2.0 * (2 ** attempt))
    raise last  # type: ignore[misc]
