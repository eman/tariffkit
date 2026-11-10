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
    body, status = _get_with_httpx(source.url)
    if body is not None:
        return _store(source, cache, body)
    if status in GONE:
        # Decided here rather than by urllib, whose answer from a publisher that
        # blocks it says nothing about the document.
        raise ExtractionError(_gone(source, status))

    request = urllib.request.Request(source.url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in GONE:
            raise ExtractionError(_gone(source, exc.code)) from exc
        raise ExtractionError(
            f"{source.url} returned HTTP {exc.code}. If this publisher blocks scripted "
            f"requests, download it in a browser and pass it with --pdf."
        ) from exc
    except urllib.error.URLError as exc:
        raise ExtractionError(f"could not reach {source.url}: {exc.reason}") from exc

    return _store(source, cache, body)


#: Statuses that mean the document is not there, as opposed to being withheld.
GONE = frozenset({404, 410})


def _gone(source: Source, status: int) -> str:
    where = (
        f"find its new link on {source.listed_on} and update the provider"
        if source.listed_on
        else "find where the publisher moved it and update the provider"
    )
    return (
        f"{source.url} returned HTTP {status}: the document is no longer there, "
        f"which usually means it was reissued under a new name -- {where}."
    )


def _get_with_httpx(url: str) -> tuple[bytes | None, int | None]:
    """Try httpx first, because some publishers reject urllib specifically.

    MCE's CDN answers urllib and curl with 403 no matter what headers they send,
    and answers httpx with 200 and the file. The difference is the client, not
    the request -- so this is worth trying before concluding a document cannot be
    downloaded.

    Returns ``(body, status)``: the body on a 200, otherwise the status httpx
    got, or None when httpx is unavailable or never got a response. The status
    matters because the urllib fallback cannot be trusted to report it: a CDN
    that blocks urllib answers 403 whether or not the document exists, so a
    404 httpx had already been given would surface as a block.
    """
    try:
        import httpx
    except ImportError:
        return None, None
    try:
        with httpx.Client(
            follow_redirects=True, timeout=TIMEOUT_SECONDS, headers=HEADERS
        ) as client:
            response = client.get(url)
    except Exception:
        return None, None
    if response.status_code != 200:
        return None, response.status_code
    return bytes(response.content), 200


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
