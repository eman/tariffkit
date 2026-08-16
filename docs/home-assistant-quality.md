# Home Assistant integration quality checklist

TariffKit's custom component (`custom_components/tariffkit/`) is not part of
Home Assistant Core and does not claim an official
[Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
tier -- that requires submission to and acceptance into Core. This page is a
self-assessment against the scale's own published rules
([checklist](https://developers.home-assistant.io/docs/core/integration-quality-scale/checklist)),
for anyone deciding whether to trust the integration, with every exemption
stated rather than left implicit.

Two things shape most of the "not applicable" rows below: the integration is
**`iot_class: calculated`** -- it prices from data you configured plus
vendored tariff tables, with no device, service, or network endpoint to poll,
authenticate to, discover, or lose connection to -- and it has a single
maintainer, so rules written for a team of code owners are read for one.

Rule text is the Quality Scale's own wording. Status reflects the code as of
this page's last revision; re-run the coverage command below before trusting
the percentages, since they drift as tests and flow branches are added.

## Bronze

| Rule | Status | Note |
|---|---|---|
| `action-setup` | Met | Actions register in `async_setup`, once per instance, before any entry loads. |
| `appropriate-polling` | Met | `DataUpdateCoordinator(update_interval=timedelta(minutes=1))`; there is nothing to poll, only a cheap local recompute, so a short interval costs nothing. |
| `brands` | Partial | Assets ship in the repository; not yet submitted to `home-assistant/brands`, so a generic icon shows until then. |
| `common-modules` | Met | Config/options schemas, the coordinator, and profile helpers are each in their own module rather than duplicated per flow step. |
| `config-flow-test-coverage` | Partial | Exercised for the manual/import branch, conditional delivery fields, multi-entry setup, and the options menu grouping, but `config_flow.py` measures 50% statement coverage today (see [Measuring test coverage](#measuring-test-coverage)) -- the CCA and history sub-flows are the largest gaps. |
| `config-flow` | Met | UI-only, no YAML. `ConfigEntry.data` holds the profile; `ConfigEntry.options` holds forecast/Predbat settings. `data_description` is not used in any step. |
| `dependency-transparency` | Met | An exact-pinned `tariffkit==0.1.0` requirement; see [The dependency](home-assistant.md#the-dependency). |
| `docs-actions` | Met | [Actions](home-assistant.md#actions). |
| `docs-triggers` | Not applicable | The integration provides no triggers. |
| `docs-conditions` | Not applicable | The integration provides no conditions. |
| `docs-high-level-description` | Met | [Home Assistant](home-assistant.md) intro and README's "Works with". |
| `docs-installation-instructions` | Met | [Install](home-assistant.md#install). |
| `docs-removal-instructions` | Met | [Removal](home-assistant.md#removal). |
| `entity-event-setup` | Met | Entities subclass `CoordinatorEntity`, which handles listener (un)registration in its own lifecycle methods; no entity manages a subscription by hand. |
| `entity-unique-id` | Met | `f"{entry.entry_id}_{description.key}"` -- stable and config-entry-scoped, never derived from tariff or dates. |
| `has-entity-name` | Met | Every entity sets `_attr_has_entity_name = True`. |
| `runtime-data` | Met | `entry.runtime_data = coordinator`, not `hass.data[DOMAIN]`. |
| `test-before-configure` | Not applicable | There is no connection to test; the form validates the chosen tariff/supplier combination against the library synchronously and shows the library's own error inline. |
| `test-before-setup` | Met | `async_setup_entry` calls `async_config_entry_first_refresh()`; a bad profile raises `UpdateFailed` from `_async_update_data`, which fails the entry with `ConfigEntryNotReady` rather than loading broken. |
| `unique-config-entry` | Met | New entries use the normalized profile name as a stable local unique ID. Tariff, PTO, supplier, and history edits cannot change it, while importing the same named account twice is rejected. |

## Silver

| Rule | Status | Note |
|---|---|---|
| `action-exceptions` | Met | Both actions raise `ServiceValidationError` with a message naming the problem; see [Actions](home-assistant.md#actions). |
| `config-entry-unloading` | Met | `async_unload_entry` unloads platforms; reload (options save, or manual reload) recreates entities cleanly. |
| `docs-configuration-parameters` | Met | [Configure](home-assistant.md#configure) and [Account history](home-assistant.md#account-history) cover every field, including which ones are conditional. |
| `docs-installation-parameters` | Met | Folded into installation instructions above; the wizard has no separate installation-time parameters beyond the profile fields. |
| `entity-unavailable` | Not applicable | Nothing marks an entity unavailable while its entry is loaded -- there is no partial-failure mode between "computes fine" and "entry fails to load entirely" for a purely local calculation. |
| `integration-owner` | Met | `manifest.json` lists `"codeowners": ["@eman"]`. |
| `log-when-unavailable` | Not applicable | No network/device dependency to go offline or reconnect. |
| `parallel-updates` | Met | `sensor.py` declares `PARALLEL_UPDATES = 0`; entities have no independent I/O to serialize. |
| `reauthentication-flow` | Not applicable | The integration never authenticates to anything. |
| `test-coverage` | **Not met** | 71% across `custom_components/tariffkit` today, well under the 95% bar; see [Measuring test coverage](#measuring-test-coverage) for the per-module breakdown and how to reproduce it. |

## Gold

| Rule | Status | Note |
|---|---|---|
| `devices` | Met | One device per config entry. |
| `diagnostics` | Met | [Diagnostics](home-assistant.md#diagnostics). |
| `discovery` | Not applicable | Nothing on the network to discover; setup is always manual or profile-import. |
| `discovery-update-info` | Not applicable | Depends on `discovery`, above. |
| `docs-data-update` | Met | The coordinator recomputes every minute from local data; documented under [Troubleshooting](home-assistant.md#troubleshooting). |
| `docs-examples` | Met | [Automation examples](home-assistant.md#automation-examples). |
| `docs-known-limitations` | Partial | Predbat's DST caveats and the missing duplicate-account check are documented where they arise (Predbat, this page's Bronze table) rather than gathered into one limitations section. |
| `docs-supported-devices` | Not applicable | There are no physical devices; the integration covers rate plans and suppliers, not hardware. |
| `docs-supported-functions` | Met | [Entities and devices](home-assistant.md#entities-and-devices). |
| `docs-troubleshooting` | Met | [Troubleshooting](home-assistant.md#troubleshooting). |
| `docs-use-cases` | Partial | Energy dashboard, EMHASS, and Predbat use cases are each covered in their own section, but the page has no single "why would I use this" overview tying them together. |
| `dynamic-devices` | Not applicable | Exactly one static device per entry; nothing is discovered or added later. |
| `entity-category` | **Not met** | All five entities are plain (no `entity_category` assignment); none is arguably diagnostic-only enough to justify one. |
| `entity-device-class` | Met | `TOU Period` uses `SensorDeviceClass.ENUM` and `Rate Forecast Through` uses `TIMESTAMP`. The three price entities set none: HA's `MONETARY` class requires `state_class: total`, which is incompatible with a continuously-changing live rate at `state_class: measurement`, so leaving it unset is the correct choice, not a gap. |
| `entity-disabled-by-default` | Not applicable | All five entities are meant to be watched or graphed; none is noisy or a minority interest that would justify disabling it by default. |
| `entity-translations` | Met | `translation_key` plus `strings.json`/`translations/en.json` names for every entity. |
| `exception-translations` | **Not met** | Action validation errors (`ServiceValidationError`) use raw f-string messages rather than `translation_domain`/`translation_key`/`translation_placeholders`. |
| `icon-translations` | Met | `icons.json`. |
| `reconfiguration-flow` | Partial | There is no formal `async_step_reconfigure`; the same outcome -- changing account, delivery, or CCA settings without removing the entry -- is reached through the options flow's **Account pricing settings** step instead. |
| `repair-issues` | Not applicable | Nothing the integration does needs a repair flow: a bad config fails setup with a translated form error instead of loading and then requiring intervention. |
| `stale-devices` | Not applicable | One static device per entry; there is nothing to go stale. |

## Platinum

| Rule | Status | Note |
|---|---|---|
| `async-dependency` | **Not met**, mitigated | `tariffkit` is a synchronous, dependency-light computation library, not an I/O client -- there is no async version to depend on. Every call goes through `hass.async_add_executor_job`, so the event loop is never blocked; that is the correct handling of a sync dependency, but it does not satisfy the rule's literal text. |
| `inject-websession` | Not applicable | `tariffkit` makes no HTTP calls; there is no websession to inject. |
| `strict-typing` | Not verified by this repository's gate | `pyproject.toml`'s `[tool.mypy]` explicitly excludes `custom_components/` from `files`, because it follows Home Assistant's own typing conventions rather than the package's stricter internal style -- see the comment beside that setting. |

## Measuring test coverage

The numbers above (`config-flow-test-coverage`, `test-coverage`) come from
running the Home Assistant test file with coverage restricted to the
component:

```bash
uv run pytest tests/test_ha_component.py \
  --cov=custom_components.tariffkit --cov-report=term-missing
```

At last measurement: 71% overall, with `config_flow.py` the largest gap at
50% (`profile.py` legacy-import helpers and several `config_flow.py` history
sub-steps are exercised only for their common path, not every branch).
`sensor.py`, `const.py`, and `coordinator.py` are all at 90%+.
