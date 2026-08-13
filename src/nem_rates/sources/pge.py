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
from datetime import date, datetime, time
from pathlib import Path
from time import sleep
from typing import Any
from uuid import uuid4

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

#: The Visualforce page that embeds the usage-export widget. Its HTML carries
#: the Opower host and bearer token, which is why it is fetched at all.
GREEN_BUTTON_PATH = "/myaccount/apex/myAcct_VF_GreenButton"
#: Where the usage platform's GraphQL API lives on its own host.
GRAPHQL_PATH = "/ei/edge/apis/dsm-graphql-v1/cws/graphql"

OPOWER_HOST = re.compile(r"https://([a-z0-9][a-z0-9.-]*\.opower\.com)")
OPOWER_TOKEN = re.compile(r"\"(eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+)\"")
OPOWER_URN = re.compile(r"(urn:opower:v1:account:[a-z]+:uuid:[0-9a-f-]+)")

#: How far back the platform will export. Its own answer, not our choice.
MAX_HISTORY_DAYS = 1095
#: The export is a job. Roughly two minutes at the default interval, which is
#: far longer than the few seconds it has taken in practice.
EXPORT_POLL_LIMIT = 60

GENERATE_EXPORT = (
    "mutation WUE_GenerateUsageExportFile("
    "$usageExportFileConfigurationInput: UsageExportFileConfigurationInput) {\n"
    "  generateUsageExportFile(\n"
    "    usageExportFileConfigurationInput: $usageExportFileConfigurationInput\n"
    "  ) {\n    uuid\n    __typename\n  }\n}"
)
EXPORT_JOB = (
    "query WUE_GetExportJob($jobUuid: ID!) {\n"
    "  exportJob(jobUuid: $jobUuid) {\n"
    "    uuid\n    result\n    isRunning\n    isFailed\n    isFinished\n    __typename\n  }\n}"
)
ACCOUNT_URN_QUERY = (
    "query WUE_GetMetadata($selectedAccount: ID) {\n"
    "  billingAccountByAuthContext(selectedAccount: $selectedAccount) {\n"
    "    id\n    __typename\n  }\n}"
)


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
        "MyAcct_customLoginLWCController",
        "login",
        captured=True,
        note="PG&E's own controller, not Salesforce's stock LightningLoginFormController. "
        "Takes username, password, startUrl, uuid, browsercookie and validationCookie; "
        "the last two are cookies the login page sets before the form is submitted.",
    ),
}

#: Cookies the login page sets and then asks to have handed back. Reading them
#: from the jar rather than inventing them is what makes the call look like the
#: page's own.
BROWSER_COOKIE = "LSKey-c$browsercookie"
VALIDATION_COOKIE = "LSKey-c$validationCookie"
#: The login form runs in its own Lightning app, not the authenticated one.
LOGIN_APP = "siteforce:loginApp2"
COMMUNITY_APP = "siteforce:communityApp"


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

    def context(self, app: str = COMMUNITY_APP) -> str:
        if not self._fwuid:
            self.bootstrap()
        return json.dumps({"mode": "PROD", "fwuid": self._fwuid, "app": app, "loaded": {}})

    # -- the one call everything else is made of -----------------------

    def apex(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        *,
        page: str = "/myaccount/s/",
        app: str = COMMUNITY_APP,
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
                "aura.context": self.context(app),
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

    def login(self, *, force: bool = False) -> None:
        """Sign in, unless a cached session is already live.

        The portal's own controller, not Salesforce's stock one, and it wants
        two cookies back that the login page sets when it is fetched -- so the
        page has to be loaded first, which :meth:`bootstrap` does anyway for the
        framework id. Skipping straight to the POST fails in a way that looks
        like bad credentials.
        """
        if not force and self.signed_in():
            return
        if self._client is None:
            raise PortalError("session is not open; use PgeSession as a context manager")

        self.bootstrap()
        jar = self._client.cookies
        self.apex(
            "login",
            {
                "username": self.settings.username,
                "password": self.settings.password,
                "startUrl": "/myaccount/s/",
                "uuid": str(uuid4()),
                "browsercookie": jar.get(BROWSER_COOKIE, ""),
                "validationCookie": jar.get(VALIDATION_COOKIE, ""),
            },
            page=LOGIN_PATH,
            app=LOGIN_APP,
        )
        # The login response carries a redirect rather than a session flag, so
        # the only honest confirmation is a call that needs authentication.
        if not self.signed_in():
            raise PortalError(
                "the sign-in call succeeded but the session is still anonymous; the "
                "credentials may be wrong, or the portal may have added a step",
                endpoint="login",
                step="login",
            )

    # -- the usage platform, which is a different system entirely -------

    def opower(self) -> tuple[str, str, str]:
        """``(host, bearer token, account urn)`` for the usage platform.

        Usage data does not live in Salesforce. It lives in Oracle Opower, behind
        a GraphQL API on its own host with its own bearer token, and the utility
        portal embeds a Visualforce page whose HTML carries all three. So they
        are scraped from that page rather than configured: the token is
        short-lived and the host is not ours to pin.
        """
        if self._client is None:
            raise PortalError("session is not open; use PgeSession as a context manager")
        response = self._client.get(GREEN_BUTTON_PATH)
        if response.status_code != 200:
            raise PortalError(
                f"the usage page answered {response.status_code}; the session has probably expired",
                step="opower",
            )
        page = response.text
        host = OPOWER_HOST.search(page)
        token = OPOWER_TOKEN.search(page)
        if not host or not token:
            raise PortalError(
                "the usage page carries no Opower host and bearer token, so either the "
                "session is not signed in or PG&E has changed the page "
                "(see audit/pge/PORTAL.md)",
                step="opower",
            )
        urn = OPOWER_URN.search(page)
        return (
            host.group(1),
            token.group(1),
            urn.group(1) if urn else self._discover_urn(host.group(1), token.group(1)),
        )

    def _discover_urn(self, host: str, token: str) -> str:
        """Ask the platform which account this token is for.

        The page does not always carry the account's urn, but every query is
        scoped by it, so it has to come from somewhere. ``billingAccountByAuthContext``
        derives it from the token itself, which is the one source that cannot
        disagree with the credentials being used.
        """
        payload = self.graphql(host, token, "", "WUE_GetMetadata", ACCOUNT_URN_QUERY, {})
        account = payload.get("billingAccountByAuthContext") or {}
        urn = account.get("id") or account.get("urn")
        if not urn:
            raise PortalError(
                "could not determine which account this session is for", step="opower"
            )
        return str(urn)

    def graphql(
        self, host: str, token: str, urn: str, operation: str, query: str, variables: Any
    ) -> dict[str, Any]:
        """One GraphQL call against the usage platform."""
        if self._client is None:
            raise PortalError("session is not open; use PgeSession as a context manager")
        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "accept": "application/json",
            "x-requested-with": "XMLHttpRequest",
        }
        if urn:
            headers["opower-selected-entities"] = urn
        response = self._client.post(
            f"https://{host}{GRAPHQL_PATH}",
            headers=headers,
            json={
                "operationName": operation,
                "query": query,
                "variables": {**variables, "locale": "en-US"}
                if isinstance(variables, dict)
                else variables,
            },
        )
        if response.status_code != 200:
            raise PortalError(
                f"{operation}: the usage platform answered {response.status_code}",
                endpoint=operation,
            )
        body = response.json()
        if body.get("errors"):
            first = body["errors"][0]
            raise PortalError(f"{operation}: {first.get('message')}", endpoint=operation)
        return dict(body.get("data") or {})

    def fetch_bytes(self, url: str) -> bytes:
        """Fetch a pre-authenticated result URL.

        The export lands in Oracle object storage behind a signed URL, so this
        deliberately sends none of our headers or cookies with it.
        """
        import httpx

        with httpx.Client(follow_redirects=True, timeout=TIMEOUT_SECONDS) as plain:
            response = plain.get(url)
        if response.status_code != 200:
            raise PortalError(
                f"the export file answered {response.status_code}", endpoint="green_button"
            )
        return bytes(response.content)


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


def time_interval(start: date, end: date) -> str:
    """The half-open-looking interval Opower expects, in Pacific wall time.

    ``2025-12-30T00:00:00-08:00/2026-01-29T23:59:59-08:00``. The offsets are the
    ones in force on each end, so a cycle spanning a daylight-saving change
    carries two different offsets -- which is exactly what the portal itself
    sends, and why this is built from zoned datetimes rather than string
    concatenation.
    """
    from ..timeutil import PACIFIC

    first = datetime.combine(start, time(0, 0, 0), PACIFIC)
    last = datetime.combine(end, time(23, 59, 59), PACIFIC)
    return f"{first.isoformat()}/{last.isoformat()}"


def _export_config(interval: str, urn: str, fmt: str = "CSV") -> dict[str, Any]:
    """The export request the portal sends, minus its localisation strings.

    The portal also sends ~80 ``messages`` entries carrying the CSV's column
    headings. They are omitted: the file is parsed by column *position* and
    recognised heading names, so supplying English defaults changes nothing, and
    reproducing eighty translated strings would be eighty things to keep in step.
    """
    return {
        "urns": [urn],
        "utilityCode": "pge",
        "format": fmt,
        "timeInterval": interval,
        "forceLegacyData": False,
        # Three years, which is what the portal offers and how far back a
        # historical bill can be re-derived from meter data.
        "maxAgeOfDataInDays": MAX_HISTORY_DAYS,
        "enableFinerResolutions": False,
        "enableServiceAgreementAliasing": True,
        "accountNicknameSource": "VMODEL",
        "displayNameStrategy": "UTILITY_ACCOUNT_NICKNAME_AS_DISPLAY_NAME_STRATEGY",
        "fileUtilityCode": "",
        "hideBillingCosts": False,
        "hideIntervalCosts": False,
        "showAccountNickname": False,
        "showAccountNumber": True,
        "showDevice": False,
        "showOnlyNetUsage": False,
        "showServicePoint": False,
        "messages": [],
        "unitsOfMeasureAllowed": [],
        "utilityServiceQuantityIdentifiersAllowed": [],
    }


def download_green_button(
    session: PgeSession, start: date, end: date, *, fmt: str = "CSV", poll_seconds: float = 2.0
) -> str:
    """The Green Button CSV for ``[start, end]``, as text.

    Returns text rather than a path so the caller decides whether it ever
    reaches disk -- it carries the customer's name and address.

    Three steps, because the portal makes it three: ask for a file, poll until
    the job finishes, then fetch the pre-authenticated URL it hands back. The
    file is a ZIP containing one CSV whose name says ``interval_data``: 15-minute
    readings, despite the archive being called DailyUsageData.
    """
    host, token, urn = session.opower()
    job = session.graphql(
        host,
        token,
        urn,
        "WUE_GenerateUsageExportFile",
        GENERATE_EXPORT,
        {"usageExportFileConfigurationInput": _export_config(time_interval(start, end), urn, fmt)},
    )
    uuid = (job.get("generateUsageExportFile") or {}).get("uuid")
    if not uuid:
        raise PortalError("the export request returned no job id", endpoint="green_button")

    for _ in range(EXPORT_POLL_LIMIT):
        state = session.graphql(
            host,
            token,
            urn,
            "WUE_GetExportJob",
            EXPORT_JOB,
            {"jobUuid": uuid, "forceLegacyData": True, "locale": "en-US"},
        )
        report = state.get("exportJob") or {}
        if report.get("isFailed"):
            raise PortalError(f"the export job {uuid} failed", endpoint="green_button")
        if report.get("isFinished") and report.get("result"):
            return _unzip_csv(session.fetch_bytes(str(report["result"])))
        sleep(poll_seconds)

    raise PortalError(
        f"the export job {uuid} did not finish within {EXPORT_POLL_LIMIT * poll_seconds:.0f}s",
        endpoint="green_button",
    )


def _unzip_csv(payload: bytes) -> str:
    """The one CSV inside the exported archive."""
    import io
    import zipfile

    if not payload.startswith(b"PK"):
        # Served the CSV directly. Accepted rather than rejected: the shape of
        # the response is the portal's business, and either is usable.
        return payload.decode("utf-8-sig")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise PortalError(
                f"the export archive holds no CSV, only {archive.namelist()}",
                endpoint="green_button",
            )
        return archive.read(names[0]).decode("utf-8-sig")


def read_green_button_download(
    settings: PgeSettings,
    start: date,
    end: date,
    *,
    layout: GreenButtonLayout | None = None,
) -> list[IntervalReading]:
    """Download and parse Green Button data for ``[start, end]``.

    Follows the ``read_*(settings, start, end, ...)`` shape the other sources
    use. Dates rather than datetimes: a billing cycle is date-bounded, and the
    portal's own selector is a date range.
    """
    with PgeSession(settings) as session:
        session.login()
        return parse_green_button(download_green_button(session, start, end), layout)
