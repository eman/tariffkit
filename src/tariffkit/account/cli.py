"""Account-profile operations used by the command line interface.

The account model and repository deliberately know nothing about argparse or
PG&E.  This module is the narrow CLI boundary: it performs migrations,
statement reconciliation, and portal synchronization while keeping all
persisted and printed values sanitized by the public account model.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import Config
from ..errors import ConfigError
from ..secrets import get_named_secret
from .errors import AccountError
from .model import AccountEpoch, AccountObservation, AccountProfile, MeterSource, MeterSources
from .repository import NamedProfileRepository, validate_profile_name


def read_config_json(path: Path) -> Config:
    """Read a complete ``Config`` snapshot from JSON, without accepting secrets."""
    try:
        raw = sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"could not read config JSON from {path}") from exc
    if isinstance(value, Mapping) and "config" in value:
        value = value["config"]
    if not isinstance(value, Mapping):
        raise ConfigError("config JSON must contain an object")
    try:
        return Config.from_dict(dict(value))
    except (ConfigError, TypeError, ValueError) as exc:
        raise ConfigError(f"invalid config JSON: {exc}") from exc


def _config_from_audit(path: Path, *, name: str, credential_set: str | None) -> AccountProfile:
    """Convert the repository's legacy ``audit/account.toml`` representation."""
    import tomllib

    try:
        table = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise AccountError(f"could not read legacy account history {path}") from exc
    base_value = table.get("base", {})
    if not isinstance(base_value, Mapping):
        raise AccountError("legacy account history [base] must be a table")
    try:
        base = Config.from_dict(dict(base_value))
    except (ConfigError, TypeError, ValueError) as exc:
        raise AccountError(f"legacy account history has an invalid base config: {exc}") from exc

    entries = table.get("epoch", [])
    if not isinstance(entries, list):
        raise AccountError("legacy account history [[epoch]] must be an array")
    epochs: list[AccountEpoch] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise AccountError("legacy account history epochs must be tables")
        raw = dict(entry)
        start = raw.pop("from", None)
        if start is None:
            raise AccountError("legacy account history epochs need a 'from' date")
        try:
            effective = start if isinstance(start, date) else date.fromisoformat(str(start))
        except ValueError as exc:
            raise AccountError("legacy account history epoch dates must be ISO dates") from exc
        note = raw.pop("note", "")
        if not isinstance(note, str):
            raise AccountError("legacy account history epoch notes must be strings")
        merged = base.to_dict()
        merged.update(raw)
        try:
            config = Config.from_dict(merged)
        except (ConfigError, TypeError, ValueError) as exc:
            raise AccountError(
                f"legacy account history epoch {effective} has an invalid config: {exc}"
            ) from exc
        epochs.append(AccountEpoch(effective, config, note))

    if not epochs:
        raise AccountError("legacy account history has no epochs to migrate")
    return AccountProfile(
        tuple(sorted(epochs, key=lambda epoch: epoch.effective)),
        name=name,
        credential_set=credential_set,
    )


def migrate_existing(
    name: str,
    *,
    config_path: str | Path | None = None,
    audit_path: str | Path | None = None,
    effective: date | None = None,
    credential_set: str | None = None,
) -> AccountProfile:
    """Build a named profile from an explicit legacy file or the current Config.

    An explicit config path wins over an explicit audit path. Requiring the
    legacy path avoids silently reading developer-only repository state.
    """
    validate_profile_name(name)
    if config_path is None and audit_path is not None:
        candidate = Path(audit_path)
        if not candidate.is_file():
            raise ConfigError(f"legacy audit account file not found: {candidate}")
        return _config_from_audit(candidate, name=name, credential_set=credential_set)

    config = Config.load(config_path)
    return AccountProfile(
        (
            AccountEpoch(
                effective or date.today(),
                config,
            ),
        ),
        name=name,
        credential_set=credential_set,
    )


def init_profile(
    repository: NamedProfileRepository,
    name: str,
    *,
    config_path: str | Path | None = None,
    config_json: Path | None = None,
    effective: date | None = None,
    credential_set: str | None = None,
    audit_path: str | Path | None = None,
) -> AccountProfile:
    """Create a profile from explicit inputs or the resolved public configuration."""
    validate_profile_name(name)
    if name in repository.names():
        raise ConfigError(f"profile {name!r} already exists")
    if config_json is not None and config_path is not None:
        raise ConfigError("choose either --config or --config-json")
    if config_json is not None:
        config = read_config_json(config_json)
        profile = AccountProfile(
            (AccountEpoch(effective or date.today(), config),),
            name=name,
            credential_set=credential_set,
        )
    else:
        profile = migrate_existing(
            name,
            config_path=config_path,
            audit_path=audit_path,
            effective=effective,
            credential_set=credential_set,
        )
    return repository.save(name, profile)


def config_changes(args: Any) -> dict[str, object]:
    changes: dict[str, object] = {}
    for option, field in (
        ("tariff", "tariff"),
        ("supplier", "supplier"),
        ("interconnection_year", "interconnection_year"),
        ("pto_date", "pto_date"),
        ("vintage", "vintage"),
        ("acc_plus_segment", "acc_plus_segment"),
        ("discount", "discount"),
        ("base_services_charge_tier", "base_services_charge_tier"),
        ("baseline_territory", "baseline_territory"),
        ("baseline_code", "baseline_code"),
        ("nsc_rate", "nsc_rate"),
    ):
        value = getattr(args, option, None)
        if value is not None:
            changes[field] = value
    cca_json = getattr(args, "cca_json", None)
    if cca_json is not None:
        try:
            cca = json.loads(cca_json)
        except json.JSONDecodeError as exc:
            raise ConfigError("--cca-json is not valid JSON") from exc
        if not isinstance(cca, dict):
            raise ConfigError("--cca-json must contain an object")
        changes["cca"] = cca
    return changes


def update_profile(
    repository: NamedProfileRepository,
    name: str,
    *,
    effective: date,
    config_path: str | Path | None = None,
    config_json: Path | None = None,
    changes: Mapping[str, object] | None = None,
    note: str | None = None,
    credential_set: str | None = None,
    apply: bool = False,
) -> AccountProfile:
    """Create or replace one complete effective-dated Config snapshot."""
    profile = repository.load(name)
    if config_json is not None and config_path is not None:
        raise ConfigError("choose either --config or --config-json")
    if config_json is not None:
        config = read_config_json(config_json)
    elif config_path is not None:
        config = Config.load(config_path)
    else:
        try:
            current = profile.config_at(effective)
        except AccountError as exc:
            raise ConfigError(
                "an update before the first epoch needs --config or --config-json"
            ) from exc
        merged = current.to_dict()
        merged.update(dict(changes or {}))
        try:
            config = Config.from_dict(merged)
        except (ConfigError, TypeError, ValueError) as exc:
            raise ConfigError(f"invalid account update: {exc}") from exc

    epochs = list(profile.epochs)
    replacement = AccountEpoch(
        effective,
        config,
        note
        if note is not None
        else next(
            (epoch.note for epoch in epochs if epoch.effective == effective),
            "",
        ),
    )
    for index, epoch in enumerate(epochs):
        if epoch.effective == effective:
            epochs[index] = replacement
            break
    else:
        epochs.append(replacement)
    epochs.sort(key=lambda epoch: epoch.effective)
    updated = AccountProfile(
        tuple(epochs),
        name=profile.name,
        credential_set=credential_set if credential_set is not None else profile.credential_set,
        observations=profile.observations,
        meter_sources=profile.meter_sources,
    )
    return repository.save(name, updated, expected_revision=profile.revision) if apply else updated


def set_meter_source(
    repository: NamedProfileRepository,
    name: str,
    *,
    provider: str,
    grid_import_entity: str,
    grid_export_entity: str,
    apply: bool = False,
) -> AccountProfile:
    """Preview or persist one provider's profile-scoped meter mapping."""
    if provider not in ("ha", "influx"):
        raise ConfigError("meter source must be ha or influx")
    profile = repository.load(name)
    source = MeterSource(
        grid_import_entity=grid_import_entity,
        grid_export_entity=grid_export_entity,
    )
    if provider == "ha":
        sources = MeterSources(ha=source, influx=profile.meter_sources.influx)
    else:
        sources = MeterSources(ha=profile.meter_sources.ha, influx=source)
    updated = AccountProfile(
        epochs=profile.epochs,
        name=profile.name,
        credential_set=profile.credential_set,
        observations=profile.observations,
        meter_sources=sources,
    )
    return repository.save(name, updated, expected_revision=profile.revision) if apply else updated


def meter_source_summary(profile: AccountProfile, provider: str) -> dict[str, object]:
    """Return sanitized CLI data for one profile-scoped meter mapping."""
    if provider not in ("ha", "influx"):
        raise ConfigError("meter source must be ha or influx")
    source = profile.meter_sources.ha if provider == "ha" else profile.meter_sources.influx
    return {
        "profile": profile.name,
        "source": provider,
        "configured": source is not None,
        "grid_import_entity": source.grid_import_entity if source is not None else None,
        "grid_export_entity": source.grid_export_entity if source is not None else None,
    }


def apply_observations(
    repository: NamedProfileRepository,
    name: str,
    observations: Sequence[AccountObservation],
    *,
    apply: bool,
) -> tuple[AccountProfile, list[dict[str, object]]]:
    """Reconcile evidence in order and optionally persist one atomic update."""
    from ..providers.pge.reconcile import reconcile

    profile = repository.load(name)
    working = profile
    proposals: list[dict[str, object]] = []
    can_apply = True
    for observation in observations:
        proposal = reconcile(working, observation)
        proposals.append(proposal.to_dict())
        if proposal.can_apply:
            working = proposal.apply(working)
        else:
            can_apply = False

    if apply:
        if not can_apply:
            raise ConfigError("account update contains conflicts or missing required values")
        if working != profile:
            working = repository.save(name, working, expected_revision=profile.revision)
    return working, proposals


def import_statements(
    repository: NamedProfileRepository,
    name: str,
    paths: Sequence[Path],
    *,
    apply: bool,
) -> tuple[AccountProfile, list[dict[str, object]]]:
    """Parse local PDFs and reconcile only their sanitized observations."""
    from ..providers.pge.reconcile import import_statement

    observations = [import_statement(path) for path in paths]
    return apply_observations(repository, name, observations, apply=apply)


def _cache_directory(name: str) -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    parent = root / "tariffkit" / "account-sync"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    path = parent / f"{name}-{uuid4().hex}"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _row_value(row: Mapping[str, object], *needles: str) -> str | None:
    for key, value in row.items():
        normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
        if any(needle in normalized for needle in needles) and value not in (None, ""):
            return str(value)
    return None


def _row_date(row: Mapping[str, object]) -> date | None:
    for key, value in row.items():
        normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
        if not any(token in normalized for token in ("billdate", "statementdate", "invoicedate")):
            continue
        text = str(value).strip()
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            for pattern in ("%m/%d/%Y", "%m/%d/%y"):
                try:
                    from datetime import datetime

                    return datetime.strptime(text, pattern).date()
                except ValueError:
                    continue
    return None


def _pge_settings(profile: AccountProfile, config_path: str | Path | None = None) -> Any:
    from ..sources.pge import PgeSettings

    if profile.credential_set is None:
        return PgeSettings.load(config_path)
    prefix = profile.credential_set
    username = get_named_secret(prefix, "pge.username")
    password = get_named_secret(prefix, "pge.password")
    if not username or not password:
        raise ConfigError(
            f"credential set {prefix!r} does not contain both pge.username and pge.password"
        )
    settings = PgeSettings.load(config_path, username=username, password=password)
    values = {
        "browser_cookie": get_named_secret(prefix, "pge.browser_cookie"),
        "validation_cookie": get_named_secret(prefix, "pge.validation_cookie"),
        "account_urn": get_named_secret(prefix, "pge.account_urn"),
    }
    return type(settings)(
        username=settings.username,
        password=settings.password,
        account_id=settings.account_id,
        cookie_path=settings.cookie_path,
        browser_cookie=values["browser_cookie"] or settings.browser_cookie,
        validation_cookie=values["validation_cookie"] or settings.validation_cookie,
        account_urn=values["account_urn"] or settings.account_urn,
    )


def sync_profile(
    repository: NamedProfileRepository,
    name: str,
    *,
    since: date | None = None,
    apply: bool,
    keep_statements: bool = False,
    config_path: str | Path | None = None,
) -> tuple[AccountProfile, list[dict[str, object]]]:
    """Download, parse, and reconcile portal statements through a private cache."""
    from ..providers.pge.reconcile import import_statement
    from ..sources.pge import PgeSession

    profile = repository.load(name)
    cache = _cache_directory(name)
    observations: list[AccountObservation] = []
    try:
        settings = _pge_settings(profile, config_path)
        with PgeSession(settings) as session:
            # A resumed session arrives with a live session cookie and no CSRF
            # token, because the token is one-shot and deliberately not cached.
            # `login()` mints a fresh one off any authenticated page load and
            # only signs in for real when it has to, so this is cheap and does
            # not risk a device check.
            #
            # Skipping it leaves the first authenticated call to discover the
            # missing token, and `apex`'s recovery cannot rescue that one: it
            # falls back to `login(force=True)`, which fails while already
            # signed in because the login page redirects to the community and
            # the token it carries belongs to the wrong Lightning app. The
            # surface was a bare "the session token is stale" -- or, when the
            # portal answered with an empty list instead of an error, a silent
            # "received 0 statement update(s)" on an account with 25 statements.
            session.login()
            rows = session.bill_history()
            selected: list[tuple[str, str | None]] = []
            for row in rows:
                identifier = _row_value(row, "billpdf", "billid", "invoiceid", "statementid")
                if not identifier:
                    continue
                issued = _row_date(row)
                if since is not None and (issued is None or issued < since):
                    continue
                selected.append((identifier, issued.isoformat() if issued else None))
            if not selected:
                return profile, []
            for index, (identifier, _issued) in enumerate(selected):
                pdf_path = cache / f"statement-{index:04d}.pdf"
                pdf_path.write_bytes(session.download_bill(identifier))
                pdf_path.chmod(0o600)
                try:
                    observations.append(import_statement(pdf_path))
                finally:
                    if not keep_statements:
                        pdf_path.unlink(missing_ok=True)
    finally:
        if not keep_statements:
            shutil.rmtree(cache, ignore_errors=False)
    return apply_observations(repository, name, observations, apply=apply)


def profile_summary(profile: AccountProfile) -> dict[str, Any]:
    """Return sanitized data suitable for human or JSON CLI output."""
    return {
        "name": profile.name,
        "credential_set": profile.credential_set,
        "epochs": [
            {
                "effective": epoch.effective.isoformat(),
                "tariff": epoch.config.tariff,
                "supplier": epoch.config.supplier.value,
                "note": epoch.note,
            }
            for epoch in profile.epochs
        ],
        "observations": len(profile.observations),
        "meter_sources": profile.meter_sources.to_dict(),
        "revision": profile.revision,
    }


__all__ = [
    "apply_observations",
    "config_changes",
    "import_statements",
    "init_profile",
    "meter_source_summary",
    "migrate_existing",
    "profile_summary",
    "read_config_json",
    "set_meter_source",
    "sync_profile",
    "update_profile",
]
