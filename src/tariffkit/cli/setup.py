"""`tariffkit setup`: one guided pass through everything a first run needs.

Every piece of configuration already had a home -- the account, the keyring or
``.env``, ``config.toml`` -- and a command or a variable that fills it. What was
missing was anything that said which, in what order, and whether it worked. A
new user found out a Home Assistant host was unset when `bill` failed, and that
the keyring was unusable when `credentials set` did.

So this walks the same homes in dependency order -- the PG&E login first,
because reading the account off a statement needs it -- writes each answer
where the loaders already look, and tries each connection before moving on.
It is safe to run again: every prompt offers what is configured now, and Enter
keeps it.

CLI code, not library code, for the reason `availability` gives: the questions
and their remedies are about this front end.
"""

from __future__ import annotations

import getpass
import json
import os
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from typing import Any

from ..config import default_config_path, default_dotenv_path
from ..errors import ConfigError, TariffKitError
from ..secrets import load_dotenv, set_dotenv_value, set_secret

Scalar = str | int | bool


class Prompts:
    """Terminal questions, injectable so the flow can be tested without a TTY."""

    def __init__(
        self,
        ask: Callable[[str], str] = input,
        secret: Callable[[str], str] = getpass.getpass,
        say: Callable[[str], None] = print,
    ) -> None:
        self._ask = ask
        self._secret = secret
        self.say = say

    def text(self, question: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default else ""
        answer = self._ask(f"{question}{suffix}: ").strip()
        return answer or (default or "")

    def password(self, question: str, *, have: bool) -> str:
        """A secret, never echoed; Enter keeps the stored one when there is one."""
        suffix = " [stored; Enter keeps it]" if have else ""
        return self._secret(f"{question}{suffix}: ")

    def confirm(self, question: str, default: bool) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self._ask(f"{question} [{hint}]: ").strip().casefold()
            if not answer:
                return default
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            self.say("  please answer y or n")

    def choose(self, question: str, options: Mapping[str, str], default: str) -> str:
        """One of ``options`` by key, listing each with what it means."""
        self.say(question)
        for key, meaning in options.items():
            self.say(f"  {key:<10}{meaning}")
        while True:
            answer = self.text("choice", default)
            if answer in options:
                return answer
            self.say(f"  choose one of: {', '.join(options)}")

    def heading(self, title: str) -> None:
        self.say(f"\n== {title} ==")


# --- where answers are written ------------------------------------------------


def _toml_literal(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    # A JSON string is a valid TOML basic string for everything json.dumps emits.
    return json.dumps(value, ensure_ascii=False)


def write_toml_values(path: Path, section: str, values: Mapping[str, Scalar]) -> None:
    """Set keys in one ``[section]`` of a TOML file, leaving the rest as written.

    Line-based rather than parse-and-dump: the standard library reads TOML but
    cannot write it, and re-serialising would drop the comments a hand-written
    config carries. Only simple ``key = value`` lines are replaced; the result
    is parsed before it is saved, and nothing is written if it would not read
    back as intended.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    header = f"[{section}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(header)
        start = len(lines) - 1
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
        len(lines),
    )
    pending = dict(values)
    for index in range(start + 1, end):
        key = lines[index].split("=", 1)[0].strip()
        if "=" in lines[index] and key in pending:
            lines[index] = f"{key} = {_toml_literal(pending.pop(key))}"
    # New keys go after the section's last non-blank line, not after the blank
    # line that separates it from the next section.
    insert = end
    while insert > start + 1 and not lines[insert - 1].strip():
        insert -= 1
    lines[insert:insert] = [f"{key} = {_toml_literal(value)}" for key, value in pending.items()]

    text = "\n".join(lines) + "\n"
    try:
        table = tomllib.loads(text).get(section, {})
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"could not update {path}: {exc}") from exc
    if any(table.get(key) != value for key, value in values.items()):
        raise ConfigError(f"could not update [{section}] in {path}; edit it by hand")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Settings:
    """Reads and writes the non-secret settings, honouring the loaders' precedence.

    The loaders read ``config.toml``, then ``.env``, then the environment, later
    winning. Writing a host into ``config.toml`` while ``.env`` still names the
    old one would change nothing, so a value is written where it is currently
    read from, and one pinned by the real environment is reported rather than
    silently shadowed.
    """

    def __init__(self, config_path: Path, say: Callable[[str], None]) -> None:
        self.config_path = config_path
        self.dotenv_path = default_dotenv_path()
        self._say = say

    def current(self, section: str, key: str, variable: str | None) -> str | None:
        if variable and (value := os.environ.get(variable)):
            return value
        if variable and (value := load_dotenv(self.dotenv_path).get(variable)):
            return value
        if self.config_path.is_file():
            table = tomllib.loads(self.config_path.read_text(encoding="utf-8")).get(section, {})
            if key in table:
                return str(table[key])
        return None

    def save(self, section: str, values: Mapping[str, tuple[Scalar, str | None]]) -> None:
        """Write ``{key: (value, variable)}`` for one section."""
        to_toml: dict[str, Scalar] = {}
        for key, (value, variable) in values.items():
            if variable and variable in os.environ:
                if os.environ[variable] != str(value):
                    self._say(
                        f"  note: {variable} is set in your environment and wins over "
                        f"this; unset it or change it there"
                    )
                continue
            if variable and variable in load_dotenv(self.dotenv_path):
                set_dotenv_value(variable, str(value), self.dotenv_path)
                continue
            to_toml[key] = value
        if to_toml:
            write_toml_values(self.config_path, section, to_toml)


def _store_secret(prompts: Prompts, name: str, value: str) -> None:
    file = set_secret(name, value)
    where = f"{file} (no keyring available)" if file else "the keyring"
    prompts.say(f"  stored {name} in {where}")


def _ask_secret(prompts: Prompts, question: str, name: str, *, have: bool) -> bool:
    """Prompt for a secret and store it; whether one is configured afterwards."""
    value = prompts.password(question, have=have)
    if value:
        _store_secret(prompts, name, value)
        return True
    return have


# --- connection checks ----------------------------------------------------------


def check_pge(
    config_path: Path | None,
    verify_device: Callable[[Any, Any], None] | None = None,
) -> str | None:
    """``None`` when the portal signs in, otherwise why not.

    When the portal does not recognise this machine, ``verify_device`` is handed
    the open session and the challenge, and answers it in the same session --
    the code is tied to that sign-in. Without one, the challenge propagates.
    """
    from ..sources.pge import DeviceNotRecognisedError, PgeSession, PgeSettings

    try:
        settings = PgeSettings.load(config_path=config_path)
        with PgeSession(settings) as session:
            try:
                session.login()
            except DeviceNotRecognisedError as challenge:
                if verify_device is None:
                    raise
                verify_device(session, challenge)
    except DeviceNotRecognisedError:
        raise
    except Exception as exc:  # httpx's transport errors too: a failed check is an answer
        return str(exc) or type(exc).__name__
    return None


def _device_verifier(prompts: Prompts) -> Callable[[Any, Any], None]:
    """Answer PG&E's device check with a one-time code, and keep the result."""
    from ..sources.pge import PortalError

    def verify(session: Any, challenge: Any) -> None:
        prompts.say(
            "  PG&E does not recognise this machine yet, so it will send a one-time\n"
            "  code, as it does the first time you sign in from a new browser. After\n"
            "  this, the machine is trusted for 180 days."
        )
        options = {"email": f"email {challenge.email}".rstrip()}
        if challenge.phone:
            options["text"] = f"text message to {challenge.phone}"
        channel = (
            prompts.choose("Where should PG&E send the code?", options, "email")
            if len(options) > 1
            else "email"
        )
        session.send_device_code("Phone" if channel == "text" else "Email")
        prompts.say(f"  code sent to your {'phone' if channel == 'text' else 'email'}")
        while True:
            code = prompts.text("code (Enter to give up)")
            if not code:
                raise PortalError("device verification skipped")
            try:
                browser, validation = session.verify_device_code(code)
                break
            except PortalError as exc:
                if "attempts left" not in str(exc):
                    raise
                prompts.say(f"  {exc}")
        _store_secret(prompts, "pge.browser_cookie", browser)
        _store_secret(prompts, "pge.validation_cookie", validation)

    return verify


def _ha_get(host: str, token: str, path: str) -> Any:
    base = host if "://" in host else f"http://{host}"
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def check_home_assistant(host: str, token: str) -> str | None:
    try:
        _ha_get(host, token, "/api/")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return "Home Assistant rejected the token (401)"
        return f"Home Assistant answered {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"could not reach {host}: {getattr(exc, 'reason', exc)}"
    return None


def resolve_home_assistant(host: str, token: str) -> tuple[str, str | None]:
    """The address that answers, with its scheme, and why not if none does.

    An address typed without a scheme is tried as https first, then http:
    guessing plain http against a TLS port fails as a dropped connection, which
    says nothing about the scheme. Whatever answered is what gets saved, so the
    WebSocket client later picks wss or ws to match.
    """
    if "://" in host:
        return host, check_home_assistant(host, token)
    problems = []
    for scheme in ("https", "http"):
        candidate = f"{scheme}://{host}"
        problem = check_home_assistant(candidate, token)
        # A refused token still means this scheme reached Home Assistant.
        if problem is None or "rejected the token" in problem:
            return candidate, problem
        problems.append(f"{scheme}: {problem}")
    return host, "; ".join(problems)


def energy_counters(host: str, token: str) -> list[str]:
    """Entities that look like cumulative energy meters, for the user to pick from.

    Only a hint: the right pair is the one measuring the *grid* connection, which
    no attribute says, so these are listed rather than chosen.
    """
    try:
        states = _ha_get(host, token, "/api/states")
    except urllib.error.URLError, OSError, ValueError:
        return []
    found = []
    for state in states if isinstance(states, list) else []:
        attributes = state.get("attributes") or {}
        if attributes.get("device_class") == "energy" and attributes.get("state_class") in {
            "total",
            "total_increasing",
        }:
            found.append(str(state.get("entity_id")))
    return sorted(found)


def check_influx(host: str, database: str, token: str) -> str | None:
    """``None`` when InfluxDB answers a query with these settings, otherwise why not."""
    from ..sources.influx import InfluxSettings, _query

    try:
        _query(InfluxSettings(host=host, database=database, token=token), "SELECT 1")
    except (TariffKitError, OSError, RuntimeError) as exc:
        return str(exc)
    except Exception as exc:  # httpx's transport errors, without importing httpx here
        return f"could not reach InfluxDB: {exc}"
    return None


def resolve_influx(host: str, database: str, token: str) -> tuple[str, str | None]:
    """The address that answers, with its scheme, as `resolve_home_assistant` does.

    A refused token or an unknown database still means the scheme reached
    InfluxDB, so either settles it.
    """
    if "://" in host:
        return host, check_influx(host, database, token)
    problems = []
    for scheme in ("https", "http"):
        candidate = f"{scheme}://{host}"
        problem = check_influx(candidate, database, token)
        if problem is None or "refused the query" in problem:
            return candidate, problem
        problems.append(f"{scheme}: {problem}")
    return host, "; ".join(problems)


#: Columns a unit might be recorded under, depending on how the writer was set up.
_UNIT_COLUMNS = ("unit_of_measurement", "unit")


def influx_counters(host: str, database: str, token: str) -> list[str]:
    """Series that look like energy counters, for the user to pick from.

    Only a hint, as for Home Assistant. Asks the table which columns it has
    before filtering: Home Assistant's InfluxDB writer can be configured to
    record the unit or not, so filtering on kWh where there is a unit column
    and on the name otherwise is the most either layout can answer.
    """
    from ..sources.influx import DEFAULT_TABLE, InfluxSettings, _query

    settings = InfluxSettings(host=host, database=database, token=token)
    try:
        columns = {
            str(row.get("column_name"))
            for row in _query(
                settings,
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_name = '{DEFAULT_TABLE}'",
            )
        }
        unit = next((column for column in _UNIT_COLUMNS if column in columns), None)
        where = f"WHERE {unit} = 'kWh'" if unit else "WHERE entity_id LIKE '%energy%'"
        rows = _query(
            settings,
            f"SELECT DISTINCT entity_id FROM {DEFAULT_TABLE} {where} ORDER BY entity_id",
        )
    except Exception:  # a hint that cannot be had is not worth failing setup over
        return []
    return [str(row["entity_id"]) for row in rows if row.get("entity_id")]


# --- the steps ------------------------------------------------------------------


def _pge_step(prompts: Prompts, config_path: Path | None) -> bool:
    """PG&E login. Returns whether a working login is configured."""
    from .commands import _have_pge_credentials

    prompts.heading("PG&E login")
    prompts.say(
        "Optional. With it, setup reads your tariff off your latest statement, and\n"
        "`bill` can download interval data and billing period dates."
    )
    have = _have_pge_credentials(config_path)
    if not prompts.confirm("use a PG&E login?", default=True):
        return False
    from ..secrets import get_secret

    username = prompts.text(
        "PG&E username",
        os.environ.get("PGE_USERNAME")
        or load_dotenv().get("PGE_USERNAME")
        or get_secret("pge.username"),
    )
    if not username:
        return False
    _store_secret(prompts, "pge.username", username)
    if not _ask_secret(prompts, "PG&E password", "pge.password", have=have):
        return False
    while True:
        prompts.say("  signing in to check...")
        problem = check_pge(config_path, _device_verifier(prompts))
        if problem is None:
            prompts.say("  ok: PG&E accepted the login")
            return True
        prompts.say(f"  PG&E sign-in failed: {problem}")
        if not prompts.confirm("try again?", default=True):
            return False
        _ask_secret(prompts, "PG&E password", "pge.password", have=True)


def _account_step(prompts: Prompts, config_path: Path | None, have_login: bool) -> bool:
    """Create the account if there is none. Returns whether one exists afterwards."""
    from .account_commands import UNDERIVABLE, downloaded_statement, init_profile
    from .commands import _account_store, _print_profile

    prompts.heading("Account")
    store = _account_store()
    if store.exists():
        prompts.say(f"You already have an account at {store.path}; keeping it.")
        prompts.say("`tariffkit account show` prints it; `tariffkit account update` changes it.")
        return True

    options = {
        "statement": "read it from a PG&E statement PDF you have",
        "manual": "answer a few questions",
        "skip": "no account for now (rate lookups still work)",
    }
    default = "manual"
    if have_login:
        options = {"portal": "read it from your latest statement online", **options}
        default = "portal"
    how = prompts.choose("How should your account be set up?", options, default)
    if how == "skip":
        return False

    changes: dict[str, object] = {}
    statement: Path | None = None
    if how == "statement":
        while True:
            statement = Path(prompts.text("path to the statement PDF")).expanduser()
            if statement.is_file():
                break
            prompts.say(f"  no file at {statement}")
    elif how == "manual":
        changes = _ask_rate_plan(prompts)

    prompts.say("Your solar interconnection is not on a bill; Enter keeps the built-in default.")
    pto = _ask_parsed(prompts, f"{UNDERIVABLE['pto_date']} (YYYY-MM-DD)", date.fromisoformat)
    if pto is not None:
        changes["pto_date"] = pto.isoformat()
    year = _ask_parsed(prompts, f"{UNDERIVABLE['interconnection_year']} (e.g. 2025)", int)
    if year is not None:
        changes["interconnection_year"] = year
    effective: date | None = None
    if how == "manual":
        # A statement dates the account from the cycle it covers. Typed answers
        # have no such date, and today's would leave every earlier bill unpriceable.
        effective = _ask_parsed(
            prompts,
            "on this rate schedule since (YYYY-MM-DD)",
            date.fromisoformat,
            default=pto.isoformat() if pto else None,
        )

    with ExitStack() as stack:
        if how == "portal":
            prompts.say("  reading your latest PG&E statement...")
            statement = stack.enter_context(downloaded_statement(config_path=config_path))
        profile, _gaps = init_profile(
            store,
            config_path=config_path,
            changes=changes,
            from_statement=statement,
            effective=effective,
        )
    _print_profile(profile, json_output=False)
    return True


def _ask_parsed[T](
    prompts: Prompts, question: str, parse: Callable[[str], T], default: str | None = None
) -> T | None:
    """Ask until the answer parses; ``None`` when left empty."""
    while True:
        answer = prompts.text(question, default)
        if not answer:
            return None
        try:
            return parse(answer)
        except ValueError:
            prompts.say(f"  could not read {answer!r}")


def _ask_rate_plan(prompts: Prompts) -> dict[str, object]:
    from ..tariff.retail import SUPPORTED_TARIFFS

    changes: dict[str, object] = {}
    while True:
        tariff = prompts.text(f"rate schedule ({', '.join(SUPPORTED_TARIFFS)})", "E-ELEC").upper()
        if tariff in SUPPORTED_TARIFFS:
            changes["tariff"] = tariff
            break
        prompts.say("  not a schedule this prices")
    if prompts.confirm("does a CCA (e.g. MCE, SVCE, EBCE) supply your generation?", False):
        changes.update(_ask_cca(prompts))
    else:
        changes["supplier"] = "bundled"
    if territory := prompts.text("baseline territory letter from your bill (Enter to skip)"):
        changes["baseline_territory"] = territory.upper()
    return changes


def _ask_cca(prompts: Prompts) -> dict[str, object]:
    from ..providers.pge.reconcile import normalize_cca_identity
    from .account_commands import _vendored_rate_card

    name = prompts.text("CCA name as printed on your bill")
    identity = normalize_cca_identity(name)
    cca: dict[str, object] = {"name": identity}
    if _vendored_rate_card(identity, date.today()):
        cca["rate_card"] = identity.lower().replace(" ", "_")
    else:
        prompts.say(
            f"  no rate card ships for {identity}; generation will need rates "
            "supplied later (see docs/configuration.md)"
        )
    if vintage := _ask_parsed(prompts, "PCIA vintage year from your bill (Enter to skip)", int):
        cca["pcia_vintage"] = vintage
    return {"supplier": "cca", "cca": cca}


def _meter_step(
    prompts: Prompts, settings: Settings, config_path: Path | None, account: bool
) -> None:
    prompts.heading("Meter data")
    prompts.say(
        "`bill` prices your actual usage. It can read your grid import/export\n"
        "counters from Home Assistant or InfluxDB, or download them from PG&E."
    )
    choice = prompts.choose(
        "Where is your meter data?",
        {
            "ha": "Home Assistant",
            "influx": "InfluxDB 3",
            "none": "neither (PG&E downloads, or CSV exports)",
        },
        "ha" if settings.current("home_assistant", "host", "HA_HOST") else "none",
    )
    if choice == "ha":
        _home_assistant(prompts, settings, account)
    elif choice == "influx":
        _influx(prompts, settings, config_path, account)


def _home_assistant(prompts: Prompts, settings: Settings, account: bool) -> None:
    from ..secrets import get_secret

    host = prompts.text(
        "Home Assistant URL",
        settings.current("home_assistant", "host", "HA_HOST") or "http://homeassistant.local:8123",
    )
    settings.save("home_assistant", {"host": (host, "HA_HOST")})
    token = (
        os.environ.get("HA_TOKEN")
        or load_dotenv().get("HA_TOKEN")
        or get_secret("home_assistant.token")
    )
    prompts.say(
        "  a long-lived access token: your HA profile > Security > Long-lived access tokens"
    )
    while True:
        if entered := prompts.password("Home Assistant token", have=bool(token)):
            token = entered
            _store_secret(prompts, "home_assistant.token", token)
        if not token:
            prompts.say("  skipped: no token")
            return
        host, problem = resolve_home_assistant(host, token)
        if problem is None:
            settings.save("home_assistant", {"host": (host, "HA_HOST")})
            prompts.say(f"  ok: connected to Home Assistant at {host}")
            break
        prompts.say(f"  {problem}")
        if not prompts.confirm("try again?", default=True):
            return
        host = prompts.text("Home Assistant URL", host)
        settings.save("home_assistant", {"host": (host, "HA_HOST")})
    counters = energy_counters(host, token)
    if counters:
        prompts.say("Energy counters Home Assistant reports (pick the ones on your grid meter):")
        for entity in counters[:25]:
            prompts.say(f"  {entity}")
    _save_entities(prompts, settings, "ha", "home_assistant", account)


def _influx(prompts: Prompts, settings: Settings, config_path: Path | None, account: bool) -> None:
    from ..secrets import get_secret

    host = prompts.text(
        "InfluxDB URL",
        settings.current("influxdb", "host", "INFLUXDB3_HOST") or "localhost:8181",
    )
    database = prompts.text(
        "database",
        settings.current("influxdb", "database", "INFLUXDB3_DATABASE") or "homeassistant",
    )
    token = (
        os.environ.get("INFLUXDB3_AUTH_TOKEN")
        or load_dotenv().get("INFLUXDB3_AUTH_TOKEN")
        or get_secret("influxdb.token")
    )
    while True:
        if entered := prompts.password("InfluxDB token", have=bool(token)):
            token = entered
            _store_secret(prompts, "influxdb.token", token)
        if not token:
            prompts.say("  skipped: no token")
            return
        host, problem = resolve_influx(host, database, token)
        if problem is None:
            prompts.say(f"  ok: InfluxDB answered at {host}")
            break
        prompts.say(f"  {problem}")
        if not prompts.confirm("try again?", default=True):
            # Kept as typed, so the next run offers it rather than the default.
            settings.save(
                "influxdb",
                {"host": (host, "INFLUXDB3_HOST"), "database": (database, "INFLUXDB3_DATABASE")},
            )
            return
        host = prompts.text("InfluxDB URL", host)
        database = prompts.text("database", database)
    settings.save(
        "influxdb",
        {"host": (host, "INFLUXDB3_HOST"), "database": (database, "INFLUXDB3_DATABASE")},
    )
    counters = influx_counters(host, database, token)
    if counters:
        prompts.say("Energy counters InfluxDB holds (pick the ones on your grid meter):")
        for entity in counters[:25]:
            prompts.say(f"  {entity}")
    _save_entities(prompts, settings, "influx", "influxdb", account)


def _save_entities(
    prompts: Prompts, settings: Settings, provider: str, section: str, account: bool
) -> None:
    current = _current_entities(provider, section, settings)
    grid_import = prompts.text("grid import counter (energy you bought)", current[0])
    grid_export = prompts.text("grid export counter (energy you sent back)", current[1])
    if not (grid_import and grid_export):
        prompts.say("  skipped: `bill` needs both counters")
        return
    if account:
        from .account_commands import set_meter_source
        from .commands import _account_store

        set_meter_source(
            _account_store(),
            provider=provider,
            grid_import_entity=grid_import,
            grid_export_entity=grid_export,
            apply=True,
        )
        prompts.say("  saved to your account")
    else:
        settings.save(
            section,
            {"import_entity": (grid_import, None), "export_entity": (grid_export, None)},
        )
        prompts.say(f"  saved to {settings.config_path}")


def _current_entities(
    provider: str, section: str, settings: Settings
) -> tuple[str | None, str | None]:
    from .commands import _account_store

    try:
        store = _account_store()
        if store.exists():
            profile = store.load()
            source = profile.meter_sources.ha if provider == "ha" else profile.meter_sources.influx
            if source is not None:
                return source.grid_import_entity, source.grid_export_entity
    except TariffKitError:
        pass
    return (
        settings.current(section, "import_entity", None),
        settings.current(section, "export_entity", None),
    )


def _mqtt_step(prompts: Prompts, settings: Settings) -> None:
    prompts.heading("MQTT (optional)")
    prompts.say("`tariffkit mqtt` publishes prices every hour, e.g. for Home Assistant.")
    broker_now = settings.current("mqtt", "broker", "TARIFFKIT_MQTT_BROKER")
    if not prompts.confirm("set up MQTT publishing?", default=bool(broker_now)):
        return
    broker = prompts.text("broker host", broker_now or "localhost")
    port = (
        _ask_parsed(
            prompts, "port", int, settings.current("mqtt", "port", "TARIFFKIT_MQTT_PORT") or "1883"
        )
        or 1883
    )
    tls = prompts.confirm("use TLS?", default=port == 8883)
    values: dict[str, tuple[Scalar, str | None]] = {
        "broker": (broker, "TARIFFKIT_MQTT_BROKER"),
        "port": (port, "TARIFFKIT_MQTT_PORT"),
        "tls": (tls, None),
    }
    if prompts.confirm("does the broker need a username and password?", default=False):
        # MqttSettings refuses credentials over plain TCP unless told the network
        # is trusted, so ask before storing anything that would then not load.
        insecure = False
        if not tls:
            prompts.say("  credentials without TLS are only allowed on a trusted network")
            insecure = prompts.confirm("is this an isolated, trusted network?", default=False)
        if tls or insecure:
            if username := prompts.text("MQTT username"):
                _store_secret(prompts, "mqtt.username", username)
                _ask_secret(prompts, "MQTT password", "mqtt.password", have=False)
            values["allow_insecure_auth"] = (insecure, None)
        else:
            prompts.say("  skipped credentials: rerun setup and choose TLS to add them")
    settings.save("mqtt", values)
    prompts.say(f"  saved to {settings.config_path}")


def run_setup(config_path: Path | None, prompts: Prompts | None = None) -> int:
    from .commands import main

    prompts = prompts or Prompts()
    path = config_path or default_config_path()
    settings = Settings(path, prompts.say)
    prompts.say(
        "This sets up tariffkit step by step. Enter keeps what is shown in [brackets];\n"
        "run it again any time to change something. Rate lookups (`tariffkit now`)\n"
        "work without any of it."
    )
    try:
        have_login = _pge_step(prompts, config_path)
        account = _account_step(prompts, config_path, have_login)
        _meter_step(prompts, settings, config_path, account)
        _mqtt_step(prompts, settings)
    except EOFError, KeyboardInterrupt:
        prompts.say("\nsetup stopped; everything answered so far is saved.")
        return 130

    prompts.heading("Result")
    prompts.say(f"settings: {path}\nsecrets:  the keyring, or {default_dotenv_path()}\n")
    argv = ["--config", str(config_path)] if config_path else []
    main([*argv, "sources"])
    prompts.say("")
    main([*argv, "now"])
    return 0
