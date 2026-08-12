"""Getting a published document onto disk.

Split out from the extractors because it is the fragile half. Parsing a tariff
sheet is deterministic; fetching one depends on whatever the publisher's CDN
feels like doing today, and the two failure modes deserve different messages.

Downloads are cached, so re-running ``--check`` does not re-download, and a
document supplied with ``--pdf`` skips this module entirely.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from .providers import USER_AGENT, Source
from .sheets import ExtractionError

#: Enough of a browser's headers to get past the naive filters. Not enough for a
#: real WAF, which is what Source.fetchable exists to record.
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

#: Anything smaller than this is an error page, not a rate document.
MIN_PLAUSIBLE_BYTES = 4096

#: Bound every request. A publisher that hangs must not hang a scheduled job.
TIMEOUT_SECONDS = 120


def _age_days(path: Path) -> str:
    from datetime import datetime

    delta = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    days = delta.days
    return "downloaded today" if days == 0 else f"downloaded {days}d ago"


def fetch(source: Source, cache: Path, *, refresh: bool = False) -> Path:
    """Download ``source`` to ``cache``, returning the path.

    Refuses up front when the publisher is known to block scripts, so the user
    is told to fetch it by hand rather than left reading a 403 traceback.
    """
    if not source.fetchable:
        raise ExtractionError(
            f"{source.url} cannot be downloaded automatically. {source.blocked_note}"
        )
    if cache.exists() and not refresh:
        # Silence here makes a stale cache look like an unchanged upstream,
        # which is the one answer --check must never get wrong.
        age = _age_days(cache)
        print(f"    using cached {cache.name} ({age}), --refresh to re-download")
        return cache

    cache.parent.mkdir(parents=True, exist_ok=True)
    body = _get_with_httpx(source.url)
    if body is not None:
        return _store(source, cache, body)

    request = urllib.request.Request(source.url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise ExtractionError(
            f"{source.url} returned HTTP {exc.code}. If this publisher blocks scripted "
            f"requests, download it in a browser and pass it with --pdf."
        ) from exc
    except urllib.error.URLError as exc:
        raise ExtractionError(f"could not reach {source.url}: {exc.reason}") from exc

    return _store(source, cache, body)


def _get_with_httpx(url: str) -> bytes | None:
    """Try httpx first, because some publishers reject urllib specifically.

    MCE's CDN answers urllib and curl with 403 no matter what headers they send,
    and answers httpx with 200 and the file. The difference is the client, not
    the request -- so this is worth trying before concluding a document cannot be
    downloaded. Returns None when httpx is unavailable or does not succeed, and
    the urllib path then runs and produces the error message.
    """
    try:
        import httpx
    except ImportError:
        return None
    try:
        with httpx.Client(
            follow_redirects=True, timeout=TIMEOUT_SECONDS, headers=HEADERS
        ) as client:
            response = client.get(url)
    except Exception:
        return None
    if response.status_code != 200:
        return None
    return bytes(response.content)


def _store(source: Source, cache: Path, body: bytes) -> Path:
    if len(body) < MIN_PLAUSIBLE_BYTES or not body.lstrip().startswith(b"%PDF"):
        # A block page is served with 200 as often as with 403. Catching it here
        # turns "no rate table found" into something that names the real cause.
        head = body[:80].decode("utf-8", "replace").strip().replace("\n", " ")
        raise ExtractionError(
            f"{source.url} did not return a PDF ({len(body)} bytes, starts {head!r}). "
            f"This is usually a bot block; download it in a browser and pass it with --pdf."
        )
    cache.write_bytes(body)
    return cache
