"""Provider-neutral account history and statement evidence models."""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Self

from ..billing import BillingPeriod
from ..billing.engine import Segment
from ..config import Config
from ..models import Supplier
from ..timeutil import to_pacific
from .errors import AccountError

_SCHEMA_VERSION = 1
SCHEMA_VERSION = _SCHEMA_VERSION
_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f]+$")
_MASKED_ACCOUNT = re.compile(r"^\*{4}\d{1,4}$")
_EXTRACTION_MODE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_METER_ENTITY = re.compile(r"^[A-Za-z0-9_.]+$")


def _as_date(value: object, *, field_name: str) -> date:
    if isinstance(value, datetime):
        raise AccountError(f"{field_name} must be a date, not a datetime")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise AccountError(f"{field_name} must be an ISO date") from exc
    raise AccountError(f"{field_name} must be an ISO date")


def _text(value: object, *, field_name: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise AccountError(f"{field_name} must be a non-empty string")
    if not value:
        if allow_empty:
            return value
        raise AccountError(f"{field_name} must be a non-empty string")
    if not _SAFE_TEXT.fullmatch(value):
        raise AccountError(f"{field_name} contains control characters")
    return value


def _digest(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise AccountError(f"{field_name} must be a SHA-256 hex digest")
    return value.lower()


def mask_account_digits(value: str) -> str:
    """Return only a four-digit account suffix with the leading digits masked."""
    if not isinstance(value, str) or not value.isdigit():
        raise AccountError("account digits must contain only decimal digits")
    digits = value
    if not 1 <= len(digits) <= 4:
        raise AccountError("account digits must contain at most four unmasked digits")
    return f"****{digits}"


def _masked_account(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _MASKED_ACCOUNT.fullmatch(value) is None:
        raise AccountError(f"{field_name} must be masked as **** followed by at most four digits")
    return value


def _validate_config_snapshot(config: Config) -> None:
    if not isinstance(config, Config):
        raise AccountError("each account epoch needs a Config snapshot")


def _meter_entity(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _METER_ENTITY.fullmatch(value) is None:
        raise AccountError(
            f"{field_name} must be a non-empty entity identifier containing only "
            "letters, digits, underscores, and dots"
        )
    return value


@dataclass(frozen=True, slots=True)
class MeterSource:
    """The grid-import and grid-export entities for one meter provider."""

    grid_import_entity: str
    grid_export_entity: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "grid_import_entity",
            _meter_entity(self.grid_import_entity, field_name="grid_import_entity"),
        )
        object.__setattr__(
            self,
            "grid_export_entity",
            _meter_entity(self.grid_export_entity, field_name="grid_export_entity"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "grid_import_entity": self.grid_import_entity,
            "grid_export_entity": self.grid_export_entity,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> MeterSource:
        _check_keys(raw, {"grid_import_entity", "grid_export_entity"}, "meter source")
        if "grid_import_entity" not in raw or "grid_export_entity" not in raw:
            raise AccountError(
                "a meter source needs both grid_import_entity and grid_export_entity"
            )
        return cls(
            grid_import_entity=_meter_entity(
                raw["grid_import_entity"], field_name="grid_import_entity"
            ),
            grid_export_entity=_meter_entity(
                raw["grid_export_entity"], field_name="grid_export_entity"
            ),
        )


@dataclass(frozen=True, slots=True)
class MeterSources:
    """Optional provider-specific meter mappings shared by all account epochs."""

    ha: MeterSource | None = None
    influx: MeterSource | None = None

    def __post_init__(self) -> None:
        for name in ("ha", "influx"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, MeterSource):
                raise AccountError(f"meter_sources.{name} must be a MeterSource")

    @property
    def home_assistant(self) -> MeterSource | None:
        return self.ha

    @property
    def influxdb(self) -> MeterSource | None:
        return self.influx

    def to_dict(self) -> dict[str, object]:
        return {
            "ha": self.ha.to_dict() if self.ha is not None else None,
            "influx": self.influx.to_dict() if self.influx is not None else None,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> MeterSources:
        _check_keys(raw, {"ha", "influx"}, "meter sources")
        values: dict[str, MeterSource | None] = {}
        for name in ("ha", "influx"):
            value = raw.get(name)
            if value is None:
                values[name] = None
            elif isinstance(value, Mapping):
                values[name] = MeterSource.from_dict(value)
            else:
                raise AccountError(f"meter_sources.{name} must be an object or null")
        return cls(ha=values["ha"], influx=values["influx"])


@dataclass(frozen=True, slots=True)
class AccountEpoch:
    """A complete configuration in force from ``effective`` onward."""

    effective: date
    config: Config
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "effective", _as_date(self.effective, field_name="effective"))
        _validate_config_snapshot(self.config)
        if not isinstance(self.note, str) or (
            self.note and _SAFE_TEXT.fullmatch(self.note) is None
        ):
            raise AccountError("epoch note contains control characters")

    def to_dict(self) -> dict[str, object]:
        return {
            "effective": self.effective.isoformat(),
            "config": self.config.to_dict(),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> AccountEpoch:
        _check_keys(raw, {"effective", "config", "note"}, "epoch")
        if "config" not in raw or "effective" not in raw:
            raise AccountError("every account epoch needs effective and config")
        config_value = raw["config"]
        if not isinstance(config_value, Mapping):
            raise AccountError("epoch config must be an object")
        note_value = raw.get("note", "")
        if not isinstance(note_value, str):
            raise AccountError("epoch note must be a string")
        return cls(
            effective=_as_date(raw["effective"], field_name="effective"),
            config=Config.from_dict(dict(config_value)),
            note=note_value,
        )


@dataclass(frozen=True, slots=True)
class ObservedAgreement:
    """Sanitized facts extracted from one service-agreement span."""

    provider: str
    statement_date: date
    period: BillingPeriod
    tariff: str | None = None
    supplier: Supplier | None = None
    cca_identity: str | None = None
    baseline_territory: str | None = None
    pcia_vintage: int | None = None
    account_suffix: str | None = None
    extraction_mode: str = "unknown"
    source_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _text(self.provider, field_name="provider"))
        object.__setattr__(
            self,
            "statement_date",
            _as_date(self.statement_date, field_name="statement_date"),
        )
        if not isinstance(self.period, BillingPeriod):
            raise AccountError("agreement period must be a BillingPeriod")
        if self.tariff is not None:
            object.__setattr__(self, "tariff", _text(self.tariff, field_name="tariff"))
        if self.supplier is not None:
            try:
                object.__setattr__(self, "supplier", Supplier(self.supplier))
            except ValueError as exc:
                raise AccountError("agreement supplier is not supported") from exc
        if self.cca_identity is not None:
            object.__setattr__(
                self, "cca_identity", _text(self.cca_identity, field_name="cca_identity")
            )
        if self.baseline_territory is not None:
            object.__setattr__(
                self,
                "baseline_territory",
                _text(self.baseline_territory, field_name="baseline_territory"),
            )
        if self.pcia_vintage is not None and (
            not isinstance(self.pcia_vintage, int) or isinstance(self.pcia_vintage, bool)
        ):
            raise AccountError("pcia_vintage must be an integer")
        object.__setattr__(
            self,
            "account_suffix",
            _masked_account(self.account_suffix, field_name="account_suffix"),
        )
        if (
            not isinstance(self.extraction_mode, str)
            or _EXTRACTION_MODE.fullmatch(self.extraction_mode) is None
        ):
            raise AccountError(f"unsupported extraction_mode {self.extraction_mode!r}")
        if self.source_digest is not None:
            object.__setattr__(
                self,
                "source_digest",
                _digest(self.source_digest, field_name="source_digest"),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "statement_date": self.statement_date.isoformat(),
            "period": self.period.to_dict(),
            "tariff": self.tariff,
            "supplier": self.supplier.value if self.supplier is not None else None,
            "cca_identity": self.cca_identity,
            "baseline_territory": self.baseline_territory,
            "pcia_vintage": self.pcia_vintage,
            "account_suffix": self.account_suffix,
            "extraction_mode": self.extraction_mode,
            "source_digest": self.source_digest,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ObservedAgreement:
        _check_keys(
            raw,
            {
                "provider",
                "statement_date",
                "period",
                "tariff",
                "supplier",
                "cca_identity",
                "baseline_territory",
                "pcia_vintage",
                "account_suffix",
                "extraction_mode",
                "source_digest",
            },
            "observed agreement",
        )
        period_value = raw.get("period")
        if not isinstance(period_value, Mapping):
            raise AccountError("agreement period must be an object")
        supplier_value = raw.get("supplier")
        supplier = None if supplier_value is None else Supplier(str(supplier_value))
        pcia_value = raw.get("pcia_vintage")
        if pcia_value is not None and (
            not isinstance(pcia_value, int) or isinstance(pcia_value, bool)
        ):
            raise AccountError("pcia_vintage must be an integer")
        return cls(
            provider=_text(raw.get("provider"), field_name="provider"),
            statement_date=_as_date(raw.get("statement_date"), field_name="statement_date"),
            period=BillingPeriod(
                _as_date(period_value.get("start"), field_name="period.start"),
                _as_date(period_value.get("end"), field_name="period.end"),
            ),
            tariff=None if raw.get("tariff") is None else str(raw["tariff"]),
            supplier=supplier,
            cca_identity=(None if raw.get("cca_identity") is None else str(raw["cca_identity"])),
            baseline_territory=(
                None if raw.get("baseline_territory") is None else str(raw["baseline_territory"])
            ),
            pcia_vintage=pcia_value,
            account_suffix=(
                None if raw.get("account_suffix") is None else str(raw["account_suffix"])
            ),
            extraction_mode=str(raw.get("extraction_mode", "unknown")),
            source_digest=None if raw.get("source_digest") is None else str(raw["source_digest"]),
        )


@dataclass(frozen=True, slots=True)
class AccountObservation:
    """Evidence kept separately from authoritative account epochs."""

    agreements: tuple[ObservedAgreement, ...] = ()
    source_digest: str | None = None
    observed_at: date | None = None

    def __post_init__(self) -> None:
        agreements = tuple(self.agreements)
        if not agreements:
            raise AccountError("an account observation needs at least one agreement")
        if any(not isinstance(agreement, ObservedAgreement) for agreement in agreements):
            raise AccountError("observation agreements must be ObservedAgreement values")
        object.__setattr__(self, "agreements", agreements)
        if self.source_digest is not None:
            object.__setattr__(
                self,
                "source_digest",
                _digest(self.source_digest, field_name="source_digest"),
            )
        if self.observed_at is not None:
            object.__setattr__(
                self, "observed_at", _as_date(self.observed_at, field_name="observed_at")
            )

    def identity(self) -> tuple[str, ...]:
        """Stable identifiers used to make importing the same evidence idempotent."""
        digests = tuple(
            agreement.source_digest
            for agreement in self.agreements
            if agreement.source_digest is not None
        )
        if self.source_digest is not None:
            return (self.source_digest,)
        if digests:
            return tuple(sorted(digests))
        canonical = json.dumps(
            [agreement.to_dict() for agreement in self.agreements],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return (f"facts:{hashlib.sha256(canonical).hexdigest()}",)

    def to_dict(self) -> dict[str, object]:
        return {
            "agreements": [agreement.to_dict() for agreement in self.agreements],
            "source_digest": self.source_digest,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> AccountObservation:
        _check_keys(raw, {"agreements", "source_digest", "observed_at"}, "observation")
        agreements_value = raw.get("agreements")
        if not isinstance(agreements_value, list):
            raise AccountError("observation agreements must be an array")
        agreements = tuple(
            ObservedAgreement.from_dict(value)
            for value in agreements_value
            if isinstance(value, Mapping)
        )
        if len(agreements) != len(agreements_value):
            raise AccountError("observation agreements must contain objects")
        return cls(
            agreements=agreements,
            source_digest=(None if raw.get("source_digest") is None else str(raw["source_digest"])),
            observed_at=(
                None
                if raw.get("observed_at") is None
                else _as_date(raw["observed_at"], field_name="observed_at")
            ),
        )


@dataclass(frozen=True, slots=True)
class AccountProfile:
    """A named account's complete, effective-dated configuration history."""

    epochs: tuple[AccountEpoch, ...]
    name: str = ""
    credential_set: str | None = None
    observations: tuple[AccountObservation, ...] = ()
    _revision: str | None = field(default=None, repr=False, compare=False)
    meter_sources: MeterSources = field(default_factory=MeterSources)

    def __post_init__(self) -> None:
        epochs = tuple(self.epochs)
        if not epochs:
            raise AccountError("an account profile needs at least one epoch")
        if any(not isinstance(epoch, AccountEpoch) for epoch in epochs):
            raise AccountError("profile epochs must be AccountEpoch values")
        dates = tuple(epoch.effective for epoch in epochs)
        if dates != tuple(sorted(dates)):
            raise AccountError("account epoch effective dates must be sorted")
        if len(set(dates)) != len(dates):
            raise AccountError("account epoch effective dates must be unique")
        object.__setattr__(self, "epochs", epochs)
        if self.name:
            self._validate_slug(self.name)
        if self.credential_set is not None:
            self._validate_credential_set(self.credential_set)
        observations = tuple(self.observations)
        if any(not isinstance(observation, AccountObservation) for observation in observations):
            raise AccountError("profile observations must be AccountObservation values")
        unique: list[AccountObservation] = []
        seen: dict[tuple[str, ...], AccountObservation] = {}
        for observation in observations:
            identity = observation.identity()
            if identity:
                previous = seen.get(identity)
                if previous is not None:
                    if (
                        previous.source_digest != observation.source_digest
                        or previous.agreements != observation.agreements
                    ):
                        raise AccountError(
                            "profile observations with the same source identity conflict"
                        )
                    continue
                seen[identity] = observation
            unique.append(observation)
        object.__setattr__(self, "observations", tuple(unique))
        if not isinstance(self.meter_sources, MeterSources):
            raise AccountError("profile meter_sources must be MeterSources")

    @staticmethod
    def _validate_slug(value: str) -> None:
        if (
            not isinstance(value, str)
            or len(value) > 64
            or re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?", value) is None
        ):
            raise AccountError("profile name must be a lowercase slug")

    @staticmethod
    def _validate_credential_set(value: str) -> None:
        if (
            not isinstance(value, str)
            or len(value) > 64
            or re.fullmatch(r"[a-z0-9](?:[a-z0-9_.-]{0,62}[a-z0-9])?", value) is None
        ):
            raise AccountError("credential_set must be a safe name")

    @property
    def effective_dates(self) -> tuple[date, ...]:
        return tuple(epoch.effective for epoch in self.epochs)

    @property
    def revision(self) -> str | None:
        """Storage assigns revisions; in-memory profiles do not have one."""
        return self._revision

    def config_at(self, moment: date | datetime) -> Config:
        """Resolve the complete snapshot in force on a local Pacific date."""
        if isinstance(moment, datetime):
            target = to_pacific(moment).date()
        else:
            target = _as_date(moment, field_name="date")
        index = bisect_right(self.effective_dates, target) - 1
        if index < 0:
            raise AccountError(
                f"date {target} is before the first account epoch ({self.epochs[0].effective})"
            )
        return self.epochs[index].config

    def epochs_in(self, period: BillingPeriod) -> tuple[AccountEpoch, ...]:
        """Return epochs active at any point in an inclusive billing period."""
        if not isinstance(period, BillingPeriod):
            raise AccountError("period must be a BillingPeriod")
        index = bisect_right(self.effective_dates, period.start) - 1
        if index < 0:
            raise AccountError(
                f"period {period.start}..{period.end} is before the first account epoch "
                f"({self.epochs[0].effective})"
            )
        return self.epochs[index : bisect_right(self.effective_dates, period.end)]

    def segments_for(self, period: BillingPeriod) -> list[Segment]:
        """Tile a billing period into segments priced by complete snapshots."""
        applicable = self.epochs_in(period)
        segments: list[Segment] = []
        for index, epoch in enumerate(applicable):
            start = max(period.start, epoch.effective)
            next_start = (
                applicable[index + 1].effective
                if index + 1 < len(applicable)
                else period.end + timedelta(days=1)
            )
            end = min(period.end, next_start - timedelta(days=1))
            segments.append(Segment(epoch.config, BillingPeriod(start, end)))
        return segments

    def with_observation(self, observation: AccountObservation) -> Self:
        """Add evidence without changing any authoritative epoch."""
        if not isinstance(observation, AccountObservation):
            raise AccountError("observation must be an AccountObservation")
        return type(self)(
            epochs=self.epochs,
            name=self.name,
            credential_set=self.credential_set,
            observations=(*self.observations, observation),
            _revision=self._revision,
            meter_sources=self.meter_sources,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the complete versioned, JSON-compatible managed representation."""
        return {
            "schema_version": _SCHEMA_VERSION,
            "name": self.name or None,
            "credential_set": self.credential_set,
            "epochs": [epoch.to_dict() for epoch in self.epochs],
            "observations": [observation.to_dict() for observation in self.observations],
            "meter_sources": self.meter_sources.to_dict(),
        }

    def to_json(self) -> str:
        """Return the canonical versioned JSON form used by the repository."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> AccountProfile:
        _check_keys(
            raw,
            {
                "schema_version",
                "name",
                "credential_set",
                "epochs",
                "observations",
                "meter_sources",
            },
            "profile",
        )
        version = raw.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool) or version != _SCHEMA_VERSION:
            raise AccountError(f"unsupported account profile schema_version {version!r}")
        epochs_value = raw.get("epochs")
        if not isinstance(epochs_value, list):
            raise AccountError("profile epochs must be an array")
        epochs = tuple(
            AccountEpoch.from_dict(value) for value in epochs_value if isinstance(value, Mapping)
        )
        if len(epochs) != len(epochs_value):
            raise AccountError("profile epochs must contain objects")
        observations_value = raw.get("observations", [])
        if not isinstance(observations_value, list):
            raise AccountError("profile observations must be an array")
        observations = tuple(
            AccountObservation.from_dict(value)
            for value in observations_value
            if isinstance(value, Mapping)
        )
        if len(observations) != len(observations_value):
            raise AccountError("profile observations must contain objects")
        name = raw.get("name") or ""
        credential_set = raw.get("credential_set")
        if credential_set is not None and not isinstance(credential_set, str):
            raise AccountError("credential_set must be a string")
        if "meter_sources" not in raw:
            meter_sources = MeterSources()
        elif isinstance(meter_sources_value := raw["meter_sources"], Mapping):
            meter_sources = MeterSources.from_dict(meter_sources_value)
        else:
            raise AccountError("profile meter_sources must be an object")
        return cls(
            epochs=epochs,
            name=_text(name, field_name="name", allow_empty=True),
            credential_set=credential_set,
            observations=observations,
            meter_sources=meter_sources,
        )

    @classmethod
    def from_json(cls, raw: str) -> AccountProfile:
        """Parse a versioned JSON profile without accepting non-finite values."""
        try:
            value = json.loads(
                raw,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON value {token}")
                ),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise AccountError("profile JSON is invalid") from exc
        if not isinstance(value, Mapping):
            raise AccountError("profile JSON must contain an object")
        return cls.from_dict(value)


def _check_keys(raw: Mapping[str, object], allowed: set[str], label: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise AccountError(f"unknown {label} keys: {sorted(unknown)}")
