"""Account operations used by the command line interface.

The account model deliberately knows nothing about argparse or PG&E.  This
module is the narrow CLI boundary: it performs migrations, statement
reconciliation, and portal synchronization while keeping all persisted and
printed values sanitized by the public account model.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..account import (
    AccountEpoch,
    AccountError,
    AccountObservation,
    AccountProfile,
    MeterSource,
    MeterSources,
)
from ..config import Config
from ..errors import ConfigError
from .account_store import AccountStore


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


def _config_from_audit(path: Path) -> AccountProfile:
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
    )


def migrate_existing(
    *,
    config_path: str | Path | None = None,
    audit_path: str | Path | None = None,
    effective: date | None = None,
) -> AccountProfile:
    """Build the account from an explicit legacy file or the current Config.

    An explicit config path wins over an explicit audit path. Requiring the
    legacy path avoids silently reading developer-only repository state.
    """
    if config_path is None and audit_path is not None:
        candidate = Path(audit_path)
        if not candidate.is_file():
            raise ConfigError(f"legacy audit account file not found: {candidate}")
        return _config_from_audit(candidate)

    config = Config.load(config_path)
    return AccountProfile((AccountEpoch(effective or date.today(), config),))


def init_profile(
    store: AccountStore,
    *,
    config_path: str | Path | None = None,
    config_json: Path | None = None,
    effective: date | None = None,
    audit_path: str | Path | None = None,
    changes: Mapping[str, object] | None = None,
) -> AccountProfile:
    """Create the account from explicit inputs or the resolved public configuration.

    ``changes`` are the per-field flags, applied over whatever base was
    resolved. Without them the first epoch is built from the built-in defaults
    -- E-ELEC, bundled, a PTO date that belongs to one site -- which is a
    plausible account rather than the caller's, and correcting it afterwards is
    awkward: an epoch dated before the first one cannot be added without
    restating the whole config, so `init` needs to be right the first time.
    """
    if store.exists():
        raise ConfigError(
            f"an account already exists at {store.path}; 'tariffkit account update' changes it"
        )
    if config_json is not None and config_path is not None:
        raise ConfigError("choose either --config or --config-json")
    if config_json is not None:
        config = read_config_json(config_json)
        profile = AccountProfile((AccountEpoch(effective or date.today(), config),))
    else:
        profile = migrate_existing(
            config_path=config_path,
            audit_path=audit_path,
            effective=effective,
        )
    if changes:
        if changes.get("supplier") == "cca" and "cca" not in changes:
            # The library says "requires a CcaConfig", which is true and does not
            # name the flag that supplies one.
            raise ConfigError(
                "--supplier cca also needs --cca-json, e.g. "
                '--cca-json \'{"name": "MCE", "option": "light_green", '
                '"pcia_vintage": "2025"}\''
            )
        epochs = list(profile.epochs)
        merged = epochs[0].config.to_dict()
        merged.update(dict(changes))
        try:
            epochs[0] = AccountEpoch(epochs[0].effective, Config.from_dict(merged), epochs[0].note)
        except (ConfigError, TypeError, ValueError) as exc:
            raise ConfigError(f"invalid account: {exc}") from exc
        profile = replace(profile, epochs=tuple(epochs))
    return store.save(profile)


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
    store: AccountStore,
    *,
    effective: date,
    config_path: str | Path | None = None,
    config_json: Path | None = None,
    changes: Mapping[str, object] | None = None,
    note: str | None = None,
    apply: bool = False,
) -> AccountProfile:
    """Create or replace one complete effective-dated Config snapshot."""
    profile = store.load()
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
    updated = replace(profile, epochs=tuple(epochs))
    return store.save(updated, expected_revision=profile.revision) if apply else updated


def set_meter_source(
    store: AccountStore,
    *,
    provider: str,
    grid_import_entity: str,
    grid_export_entity: str,
    apply: bool = False,
) -> AccountProfile:
    """Preview or persist one provider's profile-scoped meter mapping."""
    if provider not in ("ha", "influx"):
        raise ConfigError("meter source must be ha or influx")
    profile = store.load()
    source = MeterSource(
        grid_import_entity=grid_import_entity,
        grid_export_entity=grid_export_entity,
    )
    if provider == "ha":
        sources = MeterSources(ha=source, influx=profile.meter_sources.influx)
    else:
        sources = MeterSources(ha=profile.meter_sources.ha, influx=source)
    updated = replace(profile, meter_sources=sources)
    return store.save(updated, expected_revision=profile.revision) if apply else updated


def meter_source_summary(profile: AccountProfile, provider: str) -> dict[str, object]:
    """Return sanitized CLI data for one profile-scoped meter mapping."""
    if provider not in ("ha", "influx"):
        raise ConfigError("meter source must be ha or influx")
    source = profile.meter_sources.ha if provider == "ha" else profile.meter_sources.influx
    return {
        "source": provider,
        "configured": source is not None,
        "grid_import_entity": source.grid_import_entity if source is not None else None,
        "grid_export_entity": source.grid_export_entity if source is not None else None,
    }


def apply_observations(
    store: AccountStore,
    observations: Sequence[AccountObservation],
    *,
    apply: bool,
) -> tuple[AccountProfile, list[dict[str, object]]]:
    """Reconcile evidence in order and optionally persist one atomic update."""
    from ..providers.pge.reconcile import reconcile

    profile = store.load()
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
            working = store.save(working, expected_revision=profile.revision)
    return working, proposals


def _statement_reason(err: Exception, source: str | Path) -> str:
    """The parser's message without the source prefix it already carries.

    ``read_statement`` prefixes its errors with the file it was handed, which
    for a sync is a temporary name in a cache directory the run deletes -- so
    the one identifier in the message named a file that never outlived the
    command.

    Stripping *that name*, rather than everything up to the first ``": "``.
    The punctuation was never a convention the parser kept: four of its
    messages separate the source with a space and leaked the name anyway, and
    one contains a later colon of its own, so partitioning threw away the half
    that said what went wrong and kept only the problem list.
    """
    text = str(err)
    for prefix in (f"{Path(source).name}: ", f"{Path(source).name} ", f"{source}: ", f"{source} "):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def import_statements(
    store: AccountStore,
    paths: Sequence[Path],
    *,
    apply: bool,
) -> tuple[AccountProfile, list[dict[str, object]], list[dict[str, str]]]:
    """Parse local PDFs and reconcile only their sanitized observations.

    Returns the statements that could not be read alongside the proposals, for
    the same reason :func:`sync_profile` does: a directory of statements is
    worth importing even when one of them is a document this parser has never
    seen. Here the caller named the files, so each skip is reported by its own
    path rather than by a date.
    """
    from ..providers.pge.reconcile import import_statement
    from ..providers.pge.statements.errors import StatementError

    observations = []
    skipped: list[dict[str, str]] = []
    for path in paths:
        try:
            observations.append(import_statement(path))
        except StatementError as err:
            skipped.append({"statement": str(path), "reason": _statement_reason(err, path)})
    updated, proposals = apply_observations(store, observations, apply=apply)
    return updated, proposals, skipped


def _cache_directory() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    parent = root / "tariffkit" / "account-sync"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    path = parent / uuid4().hex
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
    """The portal settings, from the one place credentials are kept.

    There used to be a second place: a profile could name a keyring "credential
    set" so several profiles shared one login. That was the landlord's case and
    it went with the named profiles -- one account reads one set of credentials.
    """
    del profile
    from ..sources.pge import PgeSettings

    return PgeSettings.load(config_path)


def sync_profile(
    store: AccountStore,
    *,
    since: date | None = None,
    apply: bool,
    keep_statements: bool = False,
    config_path: str | Path | None = None,
) -> tuple[AccountProfile, list[dict[str, object]], list[dict[str, str]]]:
    """Download, parse, and reconcile portal statements through a private cache.

    Returns the profile, the reconciliation proposals, and the statements that
    could not be read. The third is not an error: the portal lists whatever it
    lists, and one document the parser does not recognise must not cost the
    caller the twenty-four beside it.
    """
    from ..providers.pge.reconcile import import_statement
    from ..providers.pge.statements.errors import StatementError
    from ..sources.pge import PgeSession

    profile = store.load()
    cache = _cache_directory()
    observations: list[AccountObservation] = []
    skipped: list[dict[str, str]] = []
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
                return profile, [], []
            for index, (identifier, issued_on) in enumerate(selected):
                pdf_path = cache / f"statement-{index:04d}.pdf"
                pdf_path.write_bytes(session.download_bill(identifier))
                pdf_path.chmod(0o600)
                try:
                    observations.append(import_statement(pdf_path))
                except StatementError as err:
                    # One statement the parser cannot read is a statement not
                    # imported, not a failed sync. Letting it propagate threw
                    # away every observation already collected and every
                    # statement after it -- an account with 25 statements
                    # imported none of them because the newest one would not
                    # parse, and the command exited non-zero as though the
                    # portal or the credentials were at fault.
                    #
                    # Named by the date the utility issued it, not by the
                    # temporary file. `statement-0000.pdf` is a loop index
                    # inside a cache directory this function deletes on the way
                    # out, so the one identifier in the message named a file
                    # that no longer existed and said nothing about which
                    # statement to go and look at.
                    skipped.append(
                        {
                            "statement": issued_on or f"#{index}",
                            "reason": _statement_reason(err, pdf_path),
                        }
                    )
                finally:
                    if not keep_statements:
                        pdf_path.unlink(missing_ok=True)
    finally:
        if not keep_statements:
            shutil.rmtree(cache, ignore_errors=False)
    updated, proposals = apply_observations(store, observations, apply=apply)
    return updated, proposals, skipped


def profile_summary(profile: AccountProfile) -> dict[str, Any]:
    """Return sanitized data suitable for human or JSON CLI output."""
    return {
        "name": profile.name,
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
