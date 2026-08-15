"""An authenticated session against PG&E's account portal.

This module exists because a Green Button CSV fetched over HTTP is still a
metered record, which is what :mod:`tariffkit.sources` is for. It is a separate
module from :mod:`~tariffkit.sources.greenbutton` rather than part of it because
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

import base64
import binascii
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
#: Where an authenticated page keeps its CSRF token: not in the HTML, but in a
#: one-shot cookie whose *name* the page carries. The key is "tokencookie"
#: spelled backwards, which is presumably the point. The page reads the cookie
#: into its config and immediately deletes it, so a client that only greps the
#: markup finds nothing and every authenticated call comes back invalidSession.
TOKEN_COOKIE_NAME = re.compile(r'"eikoocnekot"\s*[\]:]?\s*=?\s*:?\s*"([^"]+)"')
#: Salesforce's own descriptor for invoking an Apex method.
APEX_ACTION = "aura://ApexActionController/ACTION$execute"

#: The Visualforce page that embeds the usage-export widget. Its HTML carries
#: the Opower host and bearer token, which is why it is fetched at all.
GREEN_BUTTON_PATH = "/myaccount/apex/myAcct_VF_GreenButton"
#: Where the usage platform's GraphQL API lives on its own host.
GRAPHQL_PATH = "/ei/edge/apis/dsm-graphql-v1/cws/graphql"

OPOWER_HOST = re.compile(r"https://([a-z0-9][a-z0-9.-]*\.opower\.com)")
#: Not anchored on quotes: the Visualforce shell embeds the bearer unquoted,
#: while the rendered page quotes it. Matching the JWT's own shape works for
#: both and does not care how the page chose to spell it.
OPOWER_TOKEN = re.compile(r"(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})")
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
    #: Managed-package classes need their namespace in its own field. Glued onto
    #: the class name the portal answers "No apex action available".
    namespace: str = ""
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
    "bill_history": Endpoint(
        "bill_history",
        "BusinessProcessDisplayController",
        "GenericInvoke2NoCont",
        namespace="vlocity_cmt",
        captured=True,
        note="the bill list is an OmniStudio Integration Procedure, not an Apex "
        "controller, which is why no amount of grepping for a Bill*List class "
        "finds it. Dispatches sClassName/sMethodName with a JSON string input.",
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

#: The one-shot CSRF token cookie, minted under a fresh random name on every
#: sign-in.
TOKEN_COOKIE_PREFIX = "__Host-ERIC_PROD"


def _prune(jar: Any) -> list[Any]:
    """Everything worth keeping between runs, which excludes the token cookie.

    Two separate failures come from persisting it, and caching it buys nothing
    either way because it is single-use.

    Spending it is the subtle one. The next run finds the *session* cookie
    valid, so :meth:`PgeSession.signed_in` says yes and no sign-in happens --
    but the token alongside it was already spent, and the portal re-issues one
    only to a session that asks for it. The result is a session that is
    genuinely live and cannot make a single authenticated call.

    Hoarding them is the loud one. A jar that accumulates one per run grows by
    ~340 bytes each time until the request headers no longer fit and the portal
    answers 431 -- which reads as the portal breaking rather than as the client
    slowly poisoning itself.
    """
    return [c for c in jar if not c.name.startswith(TOKEN_COOKIE_PREFIX)]


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
    #: This machine's identity to the portal. Not a second factor: the account
    #: has no MFA. The portal verifies *devices*, and a browser that has been
    #: verified once carries the result for 180 days, which is why a person
    #: never sees a challenge.
    #:
    #: These are created by the login page's own JavaScript, so they never
    #: arrive over Set-Cookie and a scripted client cannot obtain them by
    #: fetching anything. Left empty, every run looks like a brand-new device
    #: and the portal asks to verify it. Copy them once from a signed-in
    #: browser -- see audit/pge/PORTAL.md.
    browser_cookie: str = field(repr=False, default="")
    validation_cookie: str = field(repr=False, default="")
    #: Which account the usage platform answers for, e.g.
    #: ``urn:opower:v1:account:pge:uuid:...``. Every usage query is scoped by it,
    #: and the portal publishes it nowhere a client can read: the page hands it
    #: to the embedded widget. Read it once from a signed-in browser (see
    #: audit/pge/PORTAL.md), or leave it unset and let the platform try to derive
    #: it from the token.
    account_urn: str = ""

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
            username=username,
            password=password,
            account_id=account_id,
            cookie_path=cookie_path,
            browser_cookie=env.get("PGE_BROWSER_COOKIE", ""),
            validation_cookie=env.get("PGE_VALIDATION_COOKIE", ""),
            account_urn=env.get("PGE_ACCOUNT_URN", ""),
        )


class PortalError(DataError):
    """The portal did not answer the way it was captured answering."""

    def __init__(self, message: str, *, endpoint: str = "", step: str = "") -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.step = step


class InvalidSessionError(PortalError):
    """The CSRF token is stale, and here is the replacement.

    Its own type because the answer is specific and recoverable: Salesforce
    rotates the token and hands the new one back inside the rejection, so the
    call simply retries with it. Treating this as a generic failure means every
    authenticated call fails once and stays failed.
    """

    def __init__(self, message: str, *, endpoint: str = "", new_token: str = "") -> None:
        super().__init__(message, endpoint=endpoint)
        self.new_token = new_token


def _bill_rows(payload: Any) -> list[dict[str, Any]]:
    """Pull the statement rows out of an Integration Procedure's reply.

    The shape is nested and version-dependent, so this walks for the first list
    of records that looks like bills rather than hard-coding a path that a
    release note could invalidate.
    """
    wanted = ("billpdf", "invoice", "amount", "billdate", "statement", "duedate", "billid")

    # Integration Procedures return their payload as a JSON *string*, so the
    # structure to search for is one decode further in than it looks.
    if isinstance(payload, Mapping) and isinstance(payload.get("returnValue"), str):
        try:
            payload = json.loads(payload["returnValue"])
        except ValueError:
            return []
    elif isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []

    def walk(node: Any, depth: int = 0) -> list[dict[str, Any]] | None:
        if depth > 10:
            return None
        # Integration Procedures nest JSON strings inside JSON strings -- the
        # rows sit two decodes down, under a `returnValue` that decodes to an
        # `IPResult` that is itself still a string. Decoding only the outer one
        # walks straight past them and reports an empty history, which reads as
        # "this account has no bills" rather than as a parsing failure.
        if isinstance(node, str) and node[:1] in "{[":
            try:
                return walk(json.loads(node), depth + 1)
            except ValueError:
                return None
        if isinstance(node, list) and node and isinstance(node[0], dict):
            keys = " ".join(node[0]).lower()
            if any(w in keys for w in wanted):
                return [r for r in node if isinstance(r, dict)]
        if isinstance(node, Mapping):
            for value in node.values():
                found = walk(value, depth + 1)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = walk(value, depth + 1)
                if found:
                    return found
        return None

    return walk(payload) or []


def _frontdoor(answer: Any) -> str:
    """The session-handoff URL a successful sign-in returns, if there is one.

    Salesforce hands back a ``/secur/frontdoor.jsp`` link carrying a one-time
    code rather than setting the session cookie on the POST itself.
    """
    if not isinstance(answer, Mapping):
        return ""
    value = answer.get("returnValue")
    message = str(value.get("retMessage", "")) if isinstance(value, Mapping) else ""
    return message if message.startswith("http") else ""


def _check_device_trust(answer: Any, *, configured: bool) -> None:
    """Turn the portal's device-verification prompt into a useful message.

    The portal answers ``retMessage: "verifymfa :"`` when it does not recognise
    the device. That wording is misleading: it is not a second factor and does
    not mean the account has MFA enabled. It means this machine has never been
    verified, and a browser only avoids it by carrying the result of an earlier
    verification for 180 days.

    Worth being precise about, because reading it as "MFA is on" sends you off
    building a one-time-code flow for a challenge that a correctly identified
    device never sees.
    """
    if not isinstance(answer, Mapping):
        return
    value = answer.get("returnValue")
    message = str(value.get("retMessage", "")) if isinstance(value, Mapping) else ""
    if "verifymfa" not in message.lower():
        return
    raise PortalError(
        "the portal does not recognise this device"
        + (
            ", even with the configured PGE_BROWSER_COOKIE and PGE_VALIDATION_COOKIE; "
            "they may have expired (the portal trusts a device for 180 days) or been "
            "copied from a different browser profile"
            if configured
            else "; set PGE_BROWSER_COOKIE and PGE_VALIDATION_COOKIE from a signed-in "
            "browser (see audit/pge/PORTAL.md). They are created by the login page's own "
            "JavaScript, so no amount of fetching will produce them"
        )
        + ". This is device verification, not multi-factor authentication -- the account "
        "needs no second factor, and a recognised device is never challenged.",
        endpoint="login",
        step="device-trust",
    )


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
                "reaching the PG&E portal needs the 'pge' extra: pip install 'tariffkit[pge]'"
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
        except OSError, ValueError:
            return
        if not isinstance(stored, list):
            # An older format, or something else entirely. A cached session is
            # only an optimisation, so an unreadable cache is discarded rather
            # than raised over: the next call signs in again.
            return
        for entry in stored:
            if not isinstance(entry, dict) or "name" not in entry:
                continue
            self._client.cookies.set(
                str(entry["name"]),
                str(entry.get("value", "")),
                domain=str(entry.get("domain", "")),
                path=str(entry.get("path", "/")),
            )

    def _save_cookies(self) -> None:
        if self._client is None:
            return
        path = self.settings.cookie_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Keyed by name *and* domain and path, not name alone: the portal sets
        # several cookies that share a name across domains (renderCtx among
        # them), and collapsing them to a dict raises rather than silently
        # picking one.
        payload = json.dumps(
            [
                {"name": c.name, "value": c.value or "", "domain": c.domain, "path": c.path}
                for c in _prune(self._client.cookies.jar)
            ]
        )
        # 0600 via os.open: a session cookie is a bearer credential, and
        # Path.write_text would leave it world-readable under a lax umask.
        handle = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(payload)

    def refresh_token(self) -> str:
        """Mint a fresh CSRF token without signing in again.

        Any authenticated page load issues one. Separated from :meth:`login`
        because the two failures are unrelated and the cures have very
        different costs: a stale token is free to replace, whereas a re-login
        risks a device check.
        """
        if self._client is None:
            raise PortalError("session is not open; use PgeSession as a context manager")
        # Bootstrap first, deliberately. It fetches the *login* page for the
        # framework id, and doing that after minting a token invalidates the
        # one just minted -- so leaving it to happen lazily inside `apex`
        # produces a token that was valid when it was read and stale by the
        # time it is sent.
        if not self._fwuid:
            self.bootstrap()
        token = self._token_from(self._client.get("/myaccount/s/").text)
        if token:
            self._token = token
        return self._token

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

    def _token_from(self, page: str) -> str:
        """The CSRF token an authenticated page is carrying, if any."""
        if self._client is None:
            return ""
        named = TOKEN_COOKIE_NAME.search(page)
        if named:
            jar = {c.name: c.value or "" for c in self._client.cookies.jar}
            carried = jar.get(named.group(1), "")
            if carried:
                return carried
        found = TOKEN.search(page)
        return found.group(1) if found else ""

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
                        "namespace": known.namespace,
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
        try:
            return _unwrap(response, known)
        except InvalidSessionError as stale:
            # Salesforce rotates the CSRF token and returns the replacement in
            # the rejection itself. Retry once with it; a second failure is real.
            spent = self._token
            replacement = stale.new_token
            if not replacement or replacement == spent:
                # No replacement offered, which does not mean the session died:
                # a token can go stale while the session stays perfectly good,
                # and any authenticated page load mints a fresh one. Re-read
                # rather than give up, because the caller's only other move is
                # a re-login -- and on this portal a needless re-login is a
                # needless device check, which is the expensive failure.
                replacement = self._token_from(self._client.get("/myaccount/s/").text)
            if (not replacement or replacement == spent) and endpoint != "login":
                # The token cookie is one-shot. A later run finds the session
                # cookie still valid -- so `signed_in()` says yes -- while the
                # token it was issued alongside has already been spent, and no
                # page load re-mints one for a session that thinks it has it.
                # Signing in again is the only way to be issued another. Safe
                # to do unprompted *because* the device cookies are configured:
                # the portal recognises the machine and does not challenge it.
                self.login(force=True)
                replacement = self._token
            if not replacement or replacement == spent:
                raise
            self._token = replacement
            retry = self._client.post(
                AURA_PATH,
                params={"r": 1, "aura.ApexAction.execute": 1},
                data={
                    "message": json.dumps(message),
                    "aura.context": self.context(app),
                    "aura.pageURI": page,
                    "aura.token": self._token,
                },
            )
            return _unwrap(retry, known)

    def bill_history(self, history_filter: str = "BILL") -> list[dict[str, Any]]:
        """Every statement and payment the portal will list.

        Dispatched through OmniStudio rather than a plain Apex controller, which
        is why searching the page's JavaScript for a bill-list class finds
        nothing: the list is an Integration Procedure named in the payload.
        """
        if self._client is None:
            raise PortalError("session is not open; use PgeSession as a context manager")
        account = self.settings.account_id or ""
        payload = self.apex(
            "bill_history",
            {
                "sClassName": "vlocity_cmt.IntegrationProcedureService",
                "sMethodName": "MyAcct_IP_GetBillPayHistoryData",
                "input": json.dumps(
                    {
                        "billingAccount": account,
                        "userProfile": "MyAcct Customer Community User",
                        "userTimeZoneName": "America/Los_Angeles",
                        "userCurrencyCode": "USD",
                        # Capital H, and it is load-bearing: the service reads
                        # this exact key and rejects an empty value rather than
                        # defaulting, so a lowercase `historyFilter` produces
                        # "Invalid value '' for query parameter historyFilter"
                        # -- an error about the *value* that is really about
                        # the key.
                        #
                        # The value is an enum whose members are nothing like
                        # the labels the page shows. "Bill Charges" in the UI
                        # is `BILL` on the wire; `Payments` is `PAYMENTS`, and
                        # there is no member for "All Activity" at all.
                        "HistoryFilter": history_filter,
                    }
                ),
                "options": json.dumps({"ignoreCache": False, "useContinuation": False}),
            },
            page="/myaccount/s/bill-and-payment-history",
        )
        return _bill_rows(payload)

    def download_bill(self, bill_id: str) -> bytes:
        """One statement, as PDF bytes.

        There is no URL for this. The portal returns the whole document
        base64-encoded inside a JSON reply and the page builds a ``blob:`` link
        client-side, so "sign in and GET the PDF" has nothing to GET -- the
        download is itself an Aura action.
        """
        payload = self.apex(
            "bill_pdf",
            {"billidfrombillhistory": bill_id},
            page="/myaccount/s/bill-and-payment-history",
        )
        # This action answers with the document wrapped one level deeper than
        # the others, so accept either shape rather than assume.
        if isinstance(payload, Mapping) and isinstance(payload.get("returnValue"), Mapping):
            payload = payload["returnValue"]
        encoded = payload.get("imageData") if isinstance(payload, Mapping) else None
        if not encoded:
            raise PortalError("the portal returned no document for this bill", endpoint="bill_pdf")
        try:
            pdf = base64.b64decode(encoded)
        except (ValueError, binascii.Error) as bad:
            raise PortalError(
                "the portal's document was not valid base64", endpoint="bill_pdf"
            ) from bad
        # Check the magic rather than trust the reply: a session that has
        # quietly expired hands back an HTML sign-in page with a 200, and
        # writing that to a .pdf turns a portal problem into a parser problem
        # several steps downstream.
        if not pdf.startswith(b"%PDF"):
            raise PortalError(
                "the portal returned something that is not a PDF; the session may have expired",
                endpoint="bill_pdf",
            )
        return pdf

    def signed_in(self) -> bool:
        """Whether the session is authenticated.

        The probe is ``isGuestUserCheck``, which answers the *opposite*
        question: it succeeds for an anonymous visitor too, and reports that
        they are a guest. Treating a successful call as proof of being signed in
        makes every anonymous session look live, so login is skipped and the
        first real request fails with a 401 far from the cause.
        """
        if self._client is None:
            return False
        # An HTTP probe rather than an Aura call. The authenticated community
        # does not publish a CSRF token in its page, so every Aura call after
        # sign-in needs a token this client cannot obtain -- and none of the
        # work it actually does needs one. What distinguishes the two states is
        # which Lightning app the portal serves.
        landing = self._client.get("/myaccount/s/")
        if landing.status_code != 200:
            return False
        if COMMUNITY_APP in landing.text and LOGIN_APP not in landing.text:
            return True

        try:
            answer = self.apex("session_check")
        except PortalError:
            return False
        # Observed shape: {"returnValue": true, "cacheable": false}, where true
        # means "yes, a guest". Other spellings are accepted because this is one
        # utility's controller and the field could be renamed.
        if isinstance(answer, Mapping):
            for key in ("returnValue", "isGuestUser", "isGuest", "guest"):
                if key in answer:
                    return not bool(answer[key])
            return False
        if isinstance(answer, bool):
            return not answer
        # An unrecognised answer is treated as "not signed in": signing in again
        # is cheap, while assuming a live session fails later and further away.
        return False

    def login(self, *, force: bool = False) -> None:
        """Sign in, unless a cached session is already live.

        The portal's own controller, not Salesforce's stock one, and it wants
        two cookies back that the login page sets when it is fetched -- so the
        page has to be loaded first, which :meth:`bootstrap` does anyway for the
        framework id. Skipping straight to the POST fails in a way that looks
        like bad credentials.
        """
        # A resumed session is the common case, and it arrives with a valid
        # session cookie and no token, because the token is one-shot and is
        # deliberately not cached. Any authenticated page load mints another,
        # so ask for one rather than signing in: submitting the login form
        # while already signed in fails outright, since the login page then
        # redirects to the community and the token it carries belongs to the
        # community app rather than to `siteforce:loginApp2`, which is the app
        # the form posts under.
        if not force and self.signed_in() and (self._token or self.refresh_token()):
            return
        if self._client is None:
            raise PortalError("session is not open; use PgeSession as a context manager")

        self.bootstrap()

        # Present this machine as the device it is. Configured values come from
        # a browser that the portal already trusts; without them the portal
        # cannot recognise the device and asks to verify it, which reads as an
        # MFA prompt on an account that has no MFA.
        browser = self.settings.browser_cookie or str(uuid4())
        validation = self.settings.validation_cookie or str(uuid4())
        for name, value in ((BROWSER_COOKIE, browser), (VALIDATION_COOKIE, validation)):
            self._client.cookies.set(name, value, domain="myaccount.pge.com", path="/")

        answer = self.apex(
            "login",
            {
                "username": self.settings.username,
                "password": self.settings.password,
                "startUrl": "/myaccount/s/",
                "uuid": str(uuid4()),
                "browsercookie": browser,
                "validationCookie": validation,
            },
            page=LOGIN_PATH,
            app=LOGIN_APP,
        )
        _check_device_trust(answer, configured=bool(self.settings.browser_cookie))

        # The call does not itself establish the session. It returns a
        # Salesforce frontdoor URL, and *following* that is what sets the
        # session cookie -- so a login that stops at a successful POST leaves an
        # anonymous client holding a success message.
        door = _frontdoor(answer)
        if door:
            self._client.get(door)
            # The authenticated community issues its own CSRF token, and the
            # one scraped from the login page stops being valid the moment the
            # session exists. Re-read it, or every later call comes back as
            # aura:invalidSession.
            landing = self._client.get("/myaccount/s/")
            token = self._token_from(landing.text)
            if token:
                self._token = token
        # The response carries a redirect rather than a session flag, so the
        # only honest confirmation is a call that needs authentication.
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
        # Configured first: the platform does not publish the account urn
        # anywhere a client can read, because the page hands it to the widget.
        if self.settings.account_urn:
            return host.group(1), token.group(1), self.settings.account_urn
        urn = OPOWER_URN.search(page)
        if urn:
            return host.group(1), token.group(1), urn.group(1)
        return host.group(1), token.group(1), self._discover_urn(host.group(1), token.group(1))

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
    # Aura prefixes its JSON with an anti-hijacking guard ("*/" or "while(1);")
    # on some responses and not others, so the payload starts at the first
    # brace rather than at byte zero.
    text = response.text
    brace = text.find("{")
    if brace < 0:
        raise PortalError(
            f"{endpoint.name}: the response carried no JSON body", endpoint=endpoint.name
        )
    try:
        # raw_decode rather than loads: the guard prefix is sometimes a comment
        # whose own brace defeats slicing, and some responses carry trailing
        # bytes after the object. This reads exactly one value and ignores the
        # rest.
        body, _ = json.JSONDecoder().raw_decode(text[brace:])
    except ValueError as exc:
        raise PortalError(
            f"{endpoint.name}: unreadable response: {exc}", endpoint=endpoint.name
        ) from exc

    # A stale CSRF token comes back as an event, not an error, and the
    # replacement travels with it.
    event = body.get("event") or {}
    if "invalidSession" in str(event.get("descriptor", "")):
        values = ((event.get("attributes") or {}).get("values")) or {}
        raise InvalidSessionError(
            f"{endpoint.name}: the session token is stale",
            endpoint=endpoint.name,
            new_token=str(values.get("newToken") or ""),
        )

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
