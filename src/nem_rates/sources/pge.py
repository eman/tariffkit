"""An authenticated session against PG&E's account portal.

This module exists because a Green Button CSV fetched over HTTP is still a
metered record, which is what :mod:`nem_rates.sources` is for. It is a separate
module from :mod:`~nem_rates.sources.greenbutton` rather than part of it because
that one documents itself as stdlib-only with no download step, and because what
is needed here is not a *format* but a *portal client* -- the same session also
serves statements, which are not this package's business.

**The portal is Salesforce Experience Cloud.** There are no REST endpoints. Every
call, including fetching a bill, is a POST to ``/s/sfsites/aura`` naming an Apex
class and method, and the response is JSON with any file base64-encoded inside
it. Anything talking to this portal has to speak Aura, which is why this is more
than a wrapper around ``httpx.get``.

Two of its inputs change out from under you and are therefore read fresh on every
session rather than stored:

``fwuid``
    The Lightning framework build id, which changes on each Salesforce release.
    Hardcoding it works until it abruptly does not.
``aura.token``
    A per-session CSRF token.

Both are scraped from the login page, which a plain client can fetch: verified
2026-08-12, HTTP 200, ``fwuid`` present, ``app`` = ``siteforce:loginApp2``.

Credentials come from the environment only, never from a config file, and are
never logged. See ``audit/pge/PORTAL.md`` for how the protocol was captured and
how to re-capture it when PG&E changes something.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..billing import IntervalReading
from ..errors import ConfigError, DataError
from .greenbutton import GreenButtonLayout, read_green_button
from .homeassistant import load_dotenv

BASE = "https://myaccount.pge.com"
LOGIN_PATH = "/myaccount/s/login/"
AURA_PATH = "/myaccount/s/sfsites/aura"

#: Where a session is cached between runs. Under .cache/, already gitignored.
DEFAULT_COOKIE_PATH = Path(".cache/pge/cookies.json")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

TIMEOUT_SECONDS = 60.0

#: The framework build id, embedded in every page's bootstrap.
FWUID = re.compile(r'"fwuid"\s*:\s*"([^"]+)"')
#: The per-session CSRF token, present once authenticated.
TOKEN = re.compile(r'"token"\s*:\s*"([^"]{20,})"')
#: Salesforce's own descriptor for invoking an Apex method.
APEX_ACTION = "aura://ApexActionController/ACTION$execute"


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One Apex action, and how sure we are that it is still the right one."""

    name: str
    classname: str
    method: str
    #: Whether this was observed answering, rather than inferred. An inferred
    #: endpoint is not a bug, but a failure against one means "check this first".
    captured: bool = False
    note: str = ""


#: Actions this client knows. Captured entries were observed in a live session;
#: see audit/pge/PORTAL.md for the procedure.
ENDPOINTS: Mapping[str, Endpoint] = {
    "session_check": Endpoint(
        "session_check",
        "MyAcct_SessionValidatorController",
        "isGuestUserCheck",
        captured=True,
        note="cheap authenticated call; used to test whether a cached session is live",
    ),
    "bill_pdf": Endpoint(
        "bill_pdf",
        "MyAcct_DownloadBillPdf",
        "httpCalloutDownloadBill",
        captured=True,
        note="takes billidfrombillhistory; returns the PDF base64-encoded in JSON",
    ),
    "login": Endpoint(
        "login",
        "LightningLoginFormController",
        "login",
        captured=False,
        note="Salesforce's stock login controller for Experience Cloud. NOT yet "
        "observed on this portal -- PG&E may use their own. `audit doctor` "
        "reports which endpoints have been confirmed.",
    ),
}


@dataclass(frozen=True, slots=True)
class PgeSettings:
    username: str
    #: Never printed. `repr=False` keeps it out of tracebacks, which render
    #: dataclass frames.
    password: str = field(repr=False, default="")
    account_id: str = ""
    cookie_path: Path = DEFAULT_COOKIE_PATH

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        dotenv_path: str | Path = ".env",
        **overrides: str | None,
    ) -> PgeSettings:
        """Mirrors ``InfluxSettings.load``: config file, then .env, then env.

        Credentials are deliberately *not* read from the config file. A config
        file is the kind of thing that gets copied into a bug report.
        """
        values: dict[str, str] = {}
        if config_path:
            import tomllib

            raw = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
            table = raw.get("pge", {})
            for key in ("account_id", "cookie_path"):
                if key in table:
                    values[key] = str(table[key])

        env = {**load_dotenv(dotenv_path), **os.environ}
        username = overrides.get("username") or env.get("PGE_USERNAME", "")
        password = overrides.get("password") or env.get("PGE_PASSWORD", "")
        account_id = (
            overrides.get("account_id")
            or env.get("PGE_ACCOUNT_ID", "")
            or values.get("account_id", "")
        )
        if not username or not password:
            raise ConfigError(
                "PG&E credentials not found; set PGE_USERNAME and PGE_PASSWORD in .env "
                "or the environment (never in a config file)"
            )
        cookie_path = Path(values.get("cookie_path", str(DEFAULT_COOKIE_PATH)))
        return cls(
            username=username, password=password, account_id=account_id, cookie_path=cookie_path
        )


class PortalError(DataError):
    """The portal did not answer the way it was captured answering."""

    def __init__(self, message: str, *, endpoint: str = "", step: str = "") -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.step = step


class PgeSession:
    """An authenticated Aura session.

    Used as a context manager. Public so the out-of-repo audit harness can reuse
    one session for both Green Button data and statements rather than logging in
    twice and holding credentials in two places.
    """

    def __init__(self, settings: PgeSettings) -> None:
        self.settings = settings
        self._client: Any = None
        self._fwuid = ""
        self._token = ""

    def __enter__(self) -> PgeSession:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise DataError(
                "reaching the PG&E portal needs the 'pge' extra: pip install 'nem-rates[pge]'"
            ) from exc
        self._client = httpx.Client(
            base_url=BASE,
            follow_redirects=True,
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
        self._load_cookies()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._client is not None:
            self._save_cookies()
            self._client.close()
            self._client = None

    # -- session state -------------------------------------------------

    def _load_cookies(self) -> None:
        path = self.settings.cookie_path
        if not path.is_file() or self._client is None:
            return
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for name, value in stored.items():
            self._client.cookies.set(name, value, domain="myaccount.pge.com")

    def _save_cookies(self) -> None:
        if self._client is None:
            return
        path = self.settings.cookie_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(dict(self._client.cookies))
        # 0600 via os.open: a session cookie is a bearer credential, and
        # Path.write_text would leave it world-readable under a lax umask.
        handle = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(payload)

    def bootstrap(self) -> tuple[str, str]:
        """Read the framework id and CSRF token off the portal.

        Read every session rather than stored: ``fwuid`` changes on each
        Salesforce release, and a stored one fails in a way that looks like a
        broken endpoint rather than a stale constant.
        """
        if self._client is None:
            raise PortalError("session is not open; use PgeSession as a context manager")
        response = self._client.get(LOGIN_PATH)
        if response.status_code != 200:
            raise PortalError(
                f"the portal answered {response.status_code} for the login page, so the "
                f"session could not be started",
                step="bootstrap",
            )
        found = FWUID.search(response.text)
        if not found:
            raise PortalError(
                "no framework id in the login page, so this is not the Aura bootstrap "
                "we captured; PG&E may have changed the portal (see audit/pge/PORTAL.md)",
                step="bootstrap",
            )
        self._fwuid = found.group(1)
        token = TOKEN.search(response.text)
        self._token = token.group(1) if token else ""
        return self._fwuid, self._token

    def context(self, app: str = "siteforce:communityApp") -> str:
        if not self._fwuid:
            self.bootstrap()
        return json.dumps({"mode": "PROD", "fwuid": self._fwuid, "app": app, "loaded": {}})

    # -- the one call everything else is made of -----------------------

    def apex(
        self, endpoint: str, params: Mapping[str, Any] | None = None, *, page: str = "/myaccount/s/"
    ) -> Any:
        """Invoke one Apex action and return its ``returnValue``."""
        if self._client is None:
            raise PortalError("session is not open; use PgeSession as a context manager")
        known = ENDPOINTS.get(endpoint)
        if known is None:
            raise PortalError(f"unknown endpoint {endpoint!r}", endpoint=endpoint)

        message = {
            "actions": [
                {
                    "id": "1;a",
                    "descriptor": APEX_ACTION,
                    "callingDescriptor": "UNKNOWN",
                    "params": {
                        "namespace": "",
                        "classname": known.classname,
                        "method": known.method,
                        "params": dict(params or {}),
                        "cacheable": False,
                        "isContinuation": False,
                    },
                }
            ]
        }
        response = self._client.post(
            AURA_PATH,
            params={"r": 1, "aura.ApexAction.execute": 1},
            data={
                "message": json.dumps(message),
                "aura.context": self.context(),
                "aura.pageURI": page,
                "aura.token": self._token,
            },
        )
        return _unwrap(response, known)

    def signed_in(self) -> bool:
        """Whether the cached session is still live."""
        try:
            self.apex("session_check")
        except PortalError:
            return False
        return True


def _unwrap(response: Any, endpoint: Endpoint) -> Any:
    """Pull the Apex return value out of an Aura response, or explain why not."""
    kind = response.headers.get("content-type", "")
    if response.status_code != 200 or "json" not in kind:
        hint = (
            "this endpoint has never been confirmed against the live portal"
            if not endpoint.captured
            else "the session may have expired, or PG&E may have changed the portal"
        )
        raise PortalError(
            f"{endpoint.name}: expected JSON, got {response.status_code} {kind or 'no type'}; "
            f"{hint} (see audit/pge/PORTAL.md)",
            endpoint=endpoint.name,
        )
    body = response.json()
    actions = body.get("actions") or []
    if not actions:
        raise PortalError(f"{endpoint.name}: no actions in the response", endpoint=endpoint.name)
    action = actions[0]
    if action.get("state") != "SUCCESS":
        errors = action.get("error") or []
        detail = errors[0].get("message") if errors else action.get("state")
        raise PortalError(f"{endpoint.name}: {detail}", endpoint=endpoint.name)
    return action.get("returnValue")


def parse_green_button(text: str, layout: GreenButtonLayout | None = None) -> list[IntervalReading]:
    """Parse downloaded Green Button CSV text.

    A thin seam over the file-based parser, which already accepts any text
    stream, so downloaded data and a file on disk go through exactly one
    implementation. Kept as its own function because that equivalence is the
    thing worth testing.
    """
    import io

    return read_green_button(io.StringIO(text), layout)


def read_green_button_download(
    settings: PgeSettings,
    start: date,
    end: date,
    *,
    layout: GreenButtonLayout | None = None,
) -> list[IntervalReading]:
    """Download and parse Green Button data for ``[start, end]``.

    Follows the ``read_*(settings, start, end, ...)`` shape the other sources
    use. Dates rather than datetimes: the portal's own selector is a date range
    and a billing cycle is date-bounded.
    """
    raise PortalError(
        "the Green Button download action has not been captured yet. Every call to this "
        "portal names an Apex class and method, and that pair can only be read off a live "
        "session -- see audit/pge/PORTAL.md for the procedure. Until then, export the CSV "
        "from the portal by hand and read it with `read_green_button`, which is unchanged.",
        endpoint="green_button",
    )
