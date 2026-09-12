# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.9.0] - 2026-09-12

### Added
- **`tariffkit account init` reads your latest bill instead of guessing.** With
  a PG&E login already stored it fetches the newest statement and sets the
  account up from it; `--from-statement <pdf>` takes a file you have, and
  `--from-portal` forces the download. With no login and no statement it still
  falls back to `config.toml` plus built-in defaults, and now says so rather
  than presenting a guess as an answer. Naming any field yourself keeps the
  download from happening at all -- a flag is an answer. A bill prints most of what an
  account is, and they are the fields most likely to be typed wrong: the tariff,
  whether generation comes from a CCA and which one, the baseline territory, and
  the PCIA vintage. The epoch is dated from the cycle the bill covers rather
  than from today, because an epoch dated today claims the account only became
  this on setup day and nothing earlier can be priced.

  Validated against 47 of this project's 48 real statements, which span three
  tariffs as the account actually changed them -- a cycle split by a rate change
  takes the schedule it *ended* on, since that is what the account now is.

  The CCA identity goes through the same normaliser the statement importer uses.
  A bill prints the marketing name -- `Marin Clean Energy` -- and lowercasing
  that gave a `rate_card` of `marin clean energy`, which can never load: the
  vendored card is `mce`. A CCA with no vendored card now gets no `rate_card` at
  all, because naming one that does not exist is worse than naming none --
  `CcaConfig` reports an incomplete CCA and the caller can supply
  `generation_rates`, where a bad card only fails to load.

  One statement, not a history: `account sync` is the tool for the history and
  better at it. And the command names what a bill cannot say -- Permission To
  Operate, the interconnection application year, ACC Plus segment, CARE or FERA
  enrolment, Base Services Charge tier -- rather than letting a default look
  like an answer.
- **`tariffkit sources`**, which reports what is configured -- including the
  account itself, which most commands need and which listing only the meters
  left off the page -- what each one would enable, and what to do about the ones
  that are not:

  ```
    yes  rates
            now, forecast, info, serve, mqtt
    no   home_assistant
            bill --source ha
            -> set HA_HOST and HA_TOKEN, or store home_assistant.token with ...
    no   pge_portal
            bill --source green-button, statement download, billing periods
            -> store pge.username and pge.password with ...
  ```

  Every source is optional and the useful combinations are not a line: an
  account with no utility login still prices a cycle from Home Assistant, one
  with no meter integration still prices a downloaded export, and one with
  neither still answers what a kilowatt-hour costs. Only rate pricing is
  unconditional, because the rate data ships in the wheel.

- **The Home Assistant setup form points at the bill-reading CLI path.** The
  integration does not contact PG&E and stores no utility login -- that is
  deliberate, and it left an HA-only user typing ten fields that a bill prints.
  The import step and the opening menu now say that `tariffkit account init`
  reads most of an account off your latest statement and `account export`
  produces the JSON to paste, with the exact commands in
  `docs/home-assistant.md`.
- **The billing cycle start day is optional on the meters step**, and says so:
  it was the one required field on a step titled "Optional", and its help said
  `0 uses the calendar month` without mentioning that statement evidence
  supersedes it entirely.
- **A repair that names the grid counters for an existing entry.** Naming them
  became part of setting up, but setup runs once: an entry created before that
  step existed never sees it, and the form lives under Configure where nobody
  looks unless they already know it is there -- so the improvement reached new
  installs only.

  Home Assistant now raises a fixable issue on any entry with no counters
  named, and the repair *is* the form: fill it in from the Repairs panel and the
  running-total entities appear, with the same validation the options flow uses.
  Naming them any other way clears the issue without anything to dismiss, and
  clearing them raises it again. Leaving both blank is still a valid answer, so
  the issue is a warning rather than an error.

  Verified against an entry created by the released 0.8.1 integration: 19
  entities and an open issue before, 30 entities and no issue after, with the
  options written where the coordinator reads them.
- **Setup asks for the grid counters**, instead of creating the entry and
  leaving the meters to be found later under Configure. A site whose counters
  are already in Home Assistant now gets its running cost, credit and net
  entities from the first screen rather than after a second trip through the
  options menu -- 30 entities against 19 on a fresh entry.

  The step is optional and skipping it is a real answer, which is what the
  earlier decision to leave meters out of setup was protecting: the counters
  are often integrated after the tariff, and a question a new user cannot
  answer yet is worse than no question. Leaving both blank creates exactly the
  entry it used to. The same validation applies as under Configure -- one
  entity named for both directions, or a `measurement` sensor with no
  cumulative change, is refused with the reason. An imported profile that
  already carries `meter_sources.ha` offers those as the suggested values.

### Changed
#### Upgrading the CLI from 0.8.1: one command, and only if you relied on a default

`bill --source ha` and `--source influx` used to fall back to a hardcoded pair
of entity names when nothing else named them. Those defaults are gone, so if
you **never** configured your counters, record them once:

```bash
tariffkit account source set ha \
  --grid-import-entity sensor.eagle_100_energy_delivered \
  --grid-export-entity sensor.eagle_100_energy_received --apply
```

```bash
tariffkit account source set influx \
  --grid-import-entity eagle_100_total_energy_delivered \
  --grid-export-entity eagle_100_total_energy_received --apply
```

Those are exactly the names 0.8.1 read. Substitute your own if they are
different -- they were one site's device and almost certainly never matched
yours, in which case `bill --source ha` was already failing to find them.

**Nothing to do** if you set `import_entity`/`export_entity` under
`[home_assistant]` or `[influxdb]` in `config.toml`, or ever ran
`tariffkit account source set`, or pass `--ha-import-entity` on the command
line, or only use `--csv` / `--source green-button`. Rate pricing -- `now`,
`forecast`, `info`, `serve`, `mqtt` -- never read a meter and is unaffected.

The error says all of this if you hit it, including the retired names, so this
note is a convenience rather than something you have to have read first. The
**Home Assistant integration needs nothing**: it never had defaults, and an
existing config entry keeps the entities it was given.
- **Choosing a source probes only as far as it has to.** `bill` asked every
  source before picking one, and each question loads a settings object that
  consults the keyring -- so a machine that only ever reads Home Assistant was
  touching the stored utility password on every run, which on a macOS Keychain
  is an access record and possibly a prompt. Choosing now stops at the first
  source that answers: three keyring lookups became none on an account
  configured for Home Assistant. `tariffkit sources` still asks everything,
  which is its whole purpose, and an unconfigured run still reports every
  source it tried.
- **`tariffkit sources` collapses the home directory** in the one path it
  prints. These lines get pasted into bug reports, and an absolute path under a
  home directory carries the account name with it.

- **A statistic does not have to belong to an entity.** The reading path never
  assumed it did -- the recorder query takes statistic ids -- but both ways of
  *configuring* one rejected the `source:object` form that an integration
  importing history writes, so a feed that priced perfectly could not be
  entered. Both are widened:

  - the meters form is a `StatisticSelector` rather than an `EntitySelector`,
    which validates an entity id and refused the colon outright. The cost is
    that it lists every statistic instead of only energy sensors, since that
    selector takes no filter; `_meter_problem` makes up for it afterwards and
    was already stricter than a picker can be, because no selector can filter
    on `state_class`.
  - the account profile's meter mapping accepts a colon, having allowed only
    letters, digits, underscores and dots.

  An id that is neither shape is still refused, so widening did not turn the
  check off. No particular feed is supported and none is planned; what is
  pinned by test is that none is shut out.

- **The library no longer decides where consumption data comes from.** Rate
  data is vendored because a tariff is the same everywhere; a meter is not, and
  which one to read is the client's answer. The engine boundary was already
  right -- `tariffkit.billing` has never imported `tariffkit.sources` -- but
  three things around it were not:

  - `tariffkit.sources.availability`, added earlier in this cycle, was library
    code whose remedies named `tariffkit` commands. It is now
    `tariffkit.cli.availability`, beside the front end whose vocabulary it
    speaks, and reader construction moved with it to `tariffkit.cli.meters`.
    The Home Assistant integration answers the same question in its own terms,
    with an entity picker.
  - **`tariffkit.metering`** is new: `interval_energy`, `carry`, `monotonic`
    and `MAX_INTERVAL_KW`, the arithmetic that turns a cumulative counter into
    interval energy and recognises the drop-to-zero artefact. These were inside
    the Home Assistant client, so the Home Assistant *integration* imported a
    websocket client to reuse three functions that make no network calls, and
    the InfluxDB client kept its own half of the same idea. No dependency, no
    extra, no client.
  - `load_dotenv` moved from the Home Assistant client to `tariffkit.secrets`,
    which is where "what is configured" already lives. InfluxDB, PG&E and the
    MQTT publisher were each importing Home Assistant to parse a text file. It
    is still importable from its old home.

  `PgeSettings`, `HaSettings` and `InfluxSettings` stay in `tariffkit.sources`
  behind their existing extras -- they are useful, and the audit harness and
  CLI both use them -- but nothing in the library builds or chooses one, and
  importing `tariffkit` pulls in no client and no extra.

- **`bill` no longer knows what a source is.** It carried a branch per source
  -- Home Assistant, InfluxDB, a Green Button export, a CSV -- each loading its
  own settings, doing its own window arithmetic, and formatting its own note,
  so adding a source meant editing the code that prices bills, and the code
  that prices bills had opinions about credentials.

  `tariffkit.sources.meters` defines a `MeterReader`: given a window, it
  answers `MeterData` -- readings, one line naming itself, and optionally a
  narrower window it knows about. The command opens a reader, resolves a
  window, reads, and prices. Its body went from 145 lines to 62 and mentions no
  source at all. A reader that ships outside the library satisfies the same
  protocol.

  The library's billing was already source-agnostic -- `BillEngine` and
  `compute_segments` have only ever taken `IntervalReading`s -- so nothing
  about a computed bill changes.

- **A utility login is optional too, and `bill` picks a source that exists.**
  `--source` defaulted to the constant `ha`, so an account pricing from
  InfluxDB, or from a Green Button export it had already downloaded, was told
  that `HA_TOKEN` was not set -- naming the one source it had not configured
  rather than any of the ones it had. The default is now the first *available*
  source, still preferring Home Assistant when it is set up, because the
  account's own meter matched a statement to 0.00 kWh on a cycle where the
  utility's export was missing 30 days. With nothing configured at all, the
  error lists every source that would work and what to do for each, and says
  that rates need none of it.

  `cached_green_button` now takes `None` for its settings and serves whatever
  the cache already holds. A login is what *downloads* an export; it is not
  what reads one, and requiring it to read one meant an account that had
  fetched a year of intervals could not price any of them offline. When no
  cached file covers the window and there is no login, the error says both --
  and points at `bill --csv` for an export you already have.

  Half a window (`--start` without `--end`) is now rejected before any of this,
  so a mistyped invocation is answered with what is wrong about it rather than
  with a survey of the sources it would have needed.

- **The grid counters are optional everywhere, with no default.** The library
  used to default `import_entity`/`export_entity` to one site's hardware, so an
  install that had named no meter was indistinguishable from one configured as
  somebody else's sensors -- and the request went out to Home Assistant or
  InfluxDB either way. `HaSettings` and `InfluxSettings` now carry `None` until
  something names them.

  **Breaking** for anyone who relied on the fallback: name the entities with
  `tariffkit account source set {ha,influx} --grid-import-entity ...
  --grid-export-entity ...`, or under `[home_assistant]` / `[influxdb]` in the
  config file. `DEFAULT_IMPORT_ENTITY` and `DEFAULT_EXPORT_ENTITY` are gone from
  `tariffkit.sources`.

  Nothing that prices a rate is affected: `tariffkit now`, `forecast` and
  `info` never touched a meter and still need no configuration at all. Only
  `bill --source ha` and `--source influx` require the entities, and they now
  fail with a message naming both the missing key and the command that sets it,
  instead of quietly reading a sensor that is not yours. The Home Assistant
  integration already worked this way -- unset means no running totals, and the
  rate entities appear regardless.

- **The documentation no longer names one brand of meter reader.** Docstrings,
  guides, changelog entries and the `audit reconcile --readings` help described
  the hardware this was developed against -- a Rainforest Eagle-100 -- as
  though every reader were one. The behaviour being described is a class of
  device, not that model: readers commonly publish a monotonic counter beside a
  raw feed that drops to zero whenever the reader re-establishes its session
  with the meter, which is the artefact the filtering exists for. The text now
  says so.

  The default entity ids went with them -- see the entry above: there are no
  defaults now, and the configuration guide says outright that the entities
  are yours to name.

### Fixed
- **`tariffkit account init` can express your account.** It took no field flags,
  so the first epoch was always built from the built-in defaults -- E-ELEC,
  bundled, a PTO date belonging to one site -- and dated today. That is not a
  cosmetic default: an epoch dated *before* the first one cannot be added
  without restating the whole config, so anyone who ran `init` before knowing
  to pass something was left correcting history through `--config-json`. `init`
  now accepts the same fields `update` does, from one shared definition so the
  two cannot drift, and every one of them has help text.
- **Two documented commands put a global flag after the subcommand.**
  `docs/mqtt.md` showed `tariffkit mqtt --broker ... -v`, in a code block and
  again inside a systemd unit; `-v` is a top-level flag and argparse rejects it
  there. Both are now `tariffkit -v mqtt ...`.
- **`docs/billing.md` promised a source default** that this release removes, and
  `docs/packaging_strategy.md` still described "named account profiles" and put
  account persistence in the library rather than the CLI. The repair added for
  an entry with no counters was documented nowhere.
- **The README showed two commands that do not exist.** `tariffkit account init
  home` and `tariffkit account source home show ha` both carried a profile name
  positionally, which has not been accepted since named profiles were removed in
  0.8.0 -- the released 0.8.1 README has them, and both fail with an argparse
  error. It also listed five of the eight extras, omitting `ha`, `influx` and
  `pge`: exactly the three the meter and statement paths need, so following the
  README left `bill --source ha` uninstallable. `test_packaging.py` now parses
  **every** `tariffkit` invocation in the README, `audit/README.md` and every
  page under `docs/` against the real parser -- 86 of them, with `\`
  continuations joined and systemd/cron prefixes included -- and checks the
  extras against `pyproject.toml`, so none of this can drift again.
- **`account source show` no longer promises a default.** It printed "not
  configured; the source default will be used", describing a fallback this
  release removes.
- **`audit reconcile --readings statistics` no longer needs InfluxDB.** It
  loaded the InfluxDB settings and read the series unconditionally before
  choosing a branch, so an account configured only for Home Assistant was told
  its InfluxDB series were unset while asking for the path that does not use
  them. InfluxDB is a *comparison* on that path, and a comparison that cannot
  happen must not fail the run; asking for `--readings influx` without it still
  fails, with the same message as before.

  The check has to be `require_entities`, not just `load`: `load` validates the
  host, database and token and *not* the series names, so credentials present
  with series absent returned a settings object and the read raised anyway.
  Now one named `optional_influx`, with tests that fail if either half goes.

  A failed *read* is optional on that path too, not only missing
  configuration: a refused connection or a malformed response from the
  comparison source aborted a run that never needed it, and as a traceback
  rather than a message, since the harness does not catch the HTTP client's own
  errors. `optional_counters` skips it and the summary prints it as
  `not checked`, because a comparison that was not made is not one that agreed.
  `--readings influx` still fails hard.
- **One entity flag is enough to choose a source.** `--ha-import-entity` alone
  was ignored: the settings loader merges an override with the other entity from
  the profile or the config file, so a config file naming the export counter and
  a flag naming the import counter is a complete pair -- but requiring *both*
  flags meant the run reported Home Assistant unconfigured, silently picked
  another source, and told the user to name counters they had just named. Either
  flag now selects that source, and a genuinely incomplete pair is reported by
  name (`Home Assistant export_entity not set`) rather than by switching.
- **A statement downloaded by `account init` does not outlive the command.**
  `--from-portal` wrote the PDF into the sync cache and left it there, on
  success and on failure alike -- which is why `sync_profile` removes its own
  cache in a `finally`. It is a context manager now, so the file is gone
  whatever happens; a file you named yourself with `--from-statement` is never
  touched.
- **A stale repair aborts instead of raising.** Home Assistant builds the fix
  flow from the Repairs panel, so an issue whose config entry had since been
  removed put a traceback in front of somebody who had only clicked a row. It
  now aborts with `entry_gone` -- the translated string that was already there
  and unused.
- **`tariffkit sources` agrees with what `bill` can use.** The Green Button
  cache check globbed `*.csv` and so counted names the export cache
  deliberately skips -- a symlink, or anything that is not a `start_end.csv`
  range -- reporting the source available for a file `bill` would then refuse.
  It uses the same parser `bill` selects with.
- **An unreachable host is an error, not a traceback.** Home Assistant,
  InfluxDB and the portal all reach the network through libraries that raise
  `OSError` subclasses -- `socket.gaierror` for a typo'd host,
  `ConnectionRefusedError` for a wrong port -- and none of those is a
  `TariffKitError`, so nothing caught them. Fixing an entity name and running
  `bill` again answered with a Python stack trace.
- **Reading a statement no longer floods the terminal.** pypdf logs a warning
  per over-long whitespace run and a PG&E bill trips it dozens of times, so
  forty lines of library noise buried whatever the command was saying. Nothing
  here acts on those warnings.
- **`--supplier cca` names the flag it needs.** It failed with the library's
  `supplier='cca' requires a CcaConfig`, which is true and does not say that
  `--cca-json` is what supplies one. The error now shows the flag and an
  example.
- **Migrating a version 1 or 2 config entry keeps its meters.** The migration
  rebuilt `options` from a fixed list of two keys, so an entry that named its
  grid counters came out the other side pricing rates only -- the running-total
  entities silently stopped existing. The meter keys travel now, and only when
  they were there: writing `""` for an entry that never had them would lose the
  meters a second way, because a key present and empty is how the integration
  records a deliberate "no entity" and that suppresses an imported profile's own
  `meter_sources.ha`. Verified by migrating a real version 1 entry in a
  container: 30 entities including all twelve running totals.
- **A statistic with no running sum is refused.** The statistic equivalent of
  the `state_class` check the entity path has always had: an hourly `change`
  exists only for a summed statistic, so a mean-only one has nothing to
  difference and would price every hour as zero.
- **`bill --ha-import-entity ... --ha-export-entity ...` reads Home Assistant
  again.** Choosing the default source consulted only the config file and the
  account profile, so naming the counters as flags left Home Assistant looking
  unconfigured -- and the run either priced from a Green Button download
  instead (saying so, but only after a network round trip) or refused outright
  while telling the user to name the very counters they had just named. Naming
  a pair on the command line now selects that source, the way a CSV path
  already did. Found by adversarial review of this branch; no test covered it,
  because every existing case passed `--source` explicitly.
- **A cached Green Button export prices an open cycle without a login.** The
  guard that requires credentials sat in front of the lookup that pulls the
  window back to the last published read -- and the utility publishes a day
  behind, so a cache holding every published day never covered "through today"
  and was refused. This defeated the offline case the cache exists for; only
  explicit `--start/--end` windows ending on an already-cached day worked.
- **A non-energy statistic is refused instead of billed as kWh.** The meters
  form lists every statistic, and the recorder converts only within a unit
  class -- so a gas series in cubic metres arrived unconverted and 26 m3 priced
  as a $26.59 electricity bill. The form now asks the recorder for the
  statistic's unit class and refuses anything that is not energy. Energy in
  other units (Wh, MJ) was always converted correctly and still is.
- **`audit reconcile` reads the series named on the account profile.** It
  loaded `InfluxSettings` without the profile's mapping, which the library's
  since-removed default entity names had been masking; the default
  `--readings influx` path then failed for any account whose series live only
  on the profile.

## [0.8.1] - 2026-09-11

### Changed
- **Home Assistant 2026.8.0 is the minimum**, up from 2026.3.0. The fix below
  calls `device_registry.async_get_device_by_identifier`, which first ships in
  2026.8.0, so the floor moves with it; HACS will not offer this update to an
  older core. The old floor was never a tested one -- it was the first release
  on the declared Python patch line, which says nothing about whether anything
  ran there, and the suite has only ever pinned the 2026.8 series.

### Fixed
- **The device lookup no longer uses a deprecated registry call.** Running the
  integration in a stock Home Assistant container logged
  `Detected that custom integration 'tariffkit' calls device_registry.async_get_device,
  which is deprecated because device identifiers and connections are no longer
  unique across config entries ... This will stop working in Home Assistant
  2027.8.0`. Our identifier *is* the entry id, so the lookup was asking a
  question it already had to qualify;
  `async_get_device_by_identifier(identifier, entry_id)` asks it once. Nothing
  in the test suite could see this -- the warning comes from Home Assistant's
  own frame helper at runtime.

## [0.8.0] - 2026-09-11

### Added
- **`audit reconcile --readings {influx,statistics}`**, so the two derivations
  of one meter can be compared rather than assumed equal. Both are the same
  smart meter through different Home Assistant pipelines -- the recorder
  aggregating an entity's states into hourly buckets, against its InfluxDB
  integration writing those states as rows that get differenced -- so agreement
  between them corroborates nothing about the meter; `--green-button` is the
  option that fetches an independent record. What it is good for is finding a
  derivation bug: the two agree on a cycle's totals to the kilowatt-hour and
  disagree about which hours the energy arrived in, 19.9 kWh of export over one
  720-hour cycle across 189 hours. InfluxDB stays the default because it
  reconciles two statements of four where the statistics reconcile none, though
  scored against the time-of-use kilowatt-hours the statement prints itself both
  reproduce the import split to within 0.03 kWh.
- **Meter comparisons name the entity they read.** A delta line saying
  "statement vs influx" left the one thing a reader needs unstated -- both
  pipelines carry the unfiltered meter counters and the filtered pair, so it
  now names the entity, as in "statement vs influx:grid_export_total".
- **The ceiling that caps `credit_applied`, published** (#58). Export credits
  are scoped, so what a cycle can spend is capped bucket by bucket rather than
  by the charge total -- a cycle holding $34.78 of charges and $19.94 of credit
  can apply $8.24, because the credit is nearly all generation credit and the
  generation charges ran out. Every term of that was published except the one
  that explained it, leaving a dashboard to infer the cap from a ratio and get
  the mechanism wrong. The `credit_buckets` attribute now gives, per bucket, the
  charges it may reach, the credit it had, and what it spent, with
  `sum(charges) + non_offsettable == gross_charges` and
  `sum(applied) == credit_applied` on both the cycle and the day. The entity
  description states the per-bucket ceiling too; it previously covered only the
  annual-true-up carry.
- **[Use cases](docs/use-cases.md)**: the four questions the calculator answers
  -- a cycle without solar, a cycle with a credit bank, realized solar payback,
  and comparing rate plans on historical data -- organized by how far back each
  has to remember, with a runnable recipe and the traps for each. Chief among
  them: `Bill.total` is not what you owe, and ranking rate plans by it picks the
  wrong plan.
- **`uncompensated_kwh` on the money entities**, when a cycle has any. Net
  Billing begins at Permission To Operate, so the cycle containing PTO exports
  energy the tariff grants nothing for -- the arrangement starting, not a
  defect. The library has carried the figure since the pre-PTO note stopped
  disqualifying the whole credit bank, but nothing published it, so a dashboard
  reading `compensated_kwh` against the export meter saw a shortfall with no
  term to explain it. The attribute is absent rather than zero on cycles that
  have none, which is most of them.
- **The integration's imports are checked against the library in the tree.**
  A top-level `from tariffkit...` that the library no longer provides fails
  collection loudly; a function-local one -- `backfill.py` and `bank.py` each
  carry one -- does not, and would surface as an `ImportError` in somebody's
  Home Assistant instead. `test_packaging.py` now walks the integration's ASTs
  and resolves every imported name. It deliberately checks the tree, not the
  released distribution the manifest pins: the pin is the last release until a
  release commit bumps it, so comparing against PyPI would be red between every
  pair of releases, and the release itself is safe by construction because the
  wheel and the manifest are built from the same tree.

### Changed
#### Upgrading Home Assistant: re-run the backfill

Nothing has to be migrated. The config entry is still version 3, so
`async_migrate_entry` does nothing to an existing one; the account stays in the
entry at `schema_version` 1; and no entity id, unique id, device identifier or
entity name changed, so nothing is orphaned or renamed. The single-account work
below is entirely CLI-side -- the integration has always kept its account in its
own config entry and never read `~/.config/tariffkit`.

**But long-term statistics are written once and kept**, and the arithmetic
behind them changed: PCIA moved from the bonus bucket to delivery, an in-cycle
offset overrun is no longer banked, credit spend is floored at zero, the pre-PTO
note no longer disqualifies the whole bank, and meter hours that used to be
dropped are recovered (54.2 to 67.0 kWh of export on one real cycle). Rows
already written keep the old numbers while new hours use the new ones, which
reads as a step in the Energy dashboard's cost series.

Re-running the backfill rewrites them -- the rows carry the same timestamps, so
they are replaced rather than added to:

```yaml
action: tariffkit.backfill_usage
data:
  config_entry: <your entry>
response_variable: backfilled
```

With no `start` it rebuilds from the billing cycle containing your PTO date,
which is as far back as a bill means anything.

Expect the live figures to move as well -- amount due, the credit bank,
cycle-to-date. That is what most of this release is: a bank that was silently
dropped now applies, and one folded across a warning is no longer discarded
without a word.

The integration can now have the utility's own cycle boundaries, but they do
not arrive on their own: it holds no portal credentials by design, so they
travel with the profile. `tariffkit account periods --apply` records them and
`account export` carries them into **Configure -> Account history -> Import
profile**. Without the CLI they can be read off the portal's own bill-period
dropdown and typed into the exported JSON -- see [Getting the billing periods
in](docs/home-assistant.md#getting-the-billing-periods-in).

#### One account, and everything it needs in one directory

**Breaking.** Named account profiles are gone. There is one account, and every
file the CLI and the audit harness read lives in `$XDG_CONFIG_HOME/tariffkit`
(`~/.config/tariffkit` by default). Real environment variables still win over
the file, so a container or a systemd unit supplies these without one.

```
~/.config/tariffkit/
  config.toml      settings that are true now
  account.json     the agreement's dated history
  .env             INFLUXDB3_*, HA_*, PGE_* -- was ./.env, in whatever
                   directory the command happened to run from
```

**Migrating.** With one profile, nothing to do: the first command adopts
`accounts/<name>.json` automatically. With several, pick the one you want and
move it yourself, because choosing between them is a decision and guessing it
prices bills from an agreement you did not choose:

```bash
mv ~/.config/tariffkit/accounts/<the-one-you-want>.json \
   ~/.config/tariffkit/account.json
chmod 600 ~/.config/tariffkit/account.json
rm -r ~/.config/tariffkit/accounts        # once you are happy
mv .env ~/.config/tariffkit/.env          # if you kept one in a project
```

**Home Assistant needs no migration**, for this or anything else in this
release: see [above](#upgrading-home-assistant-re-run-the-backfill). The
integration stores its account in its own config entry and never read the CLI's
profiles, so nothing moves. The only visible change here is that a profile no
longer carries a `credential_set`; the integration never set one.

**Gone from the CLI:** `--account` on every command, `--credential-set`,
`tariffkit account list`, and the name argument on `account show`, `update`,
`import-statement`, `sync`, `export` and `source`. `tariffkit credentials
--set NAME` goes with it -- one account reads one set of credentials.

Why: the multiplicity earned its complexity only for someone billing several
service agreements under one login, and cost everyone else a name to invent, a
flag to remember, and a silent wrong answer when the flag was forgotten. A bill
priced from `config.toml` instead of the agreement's history read a CCA account
as bundled, which gives it one export credit bank where it has two and prices a
cycle that crossed a rate change at a single tariff. What is kept is the part
that earns its keep: the dated history, because a bill prices with the settings
in force over its own days.

#### Reading the account file is the application's job, not the library's

**Breaking, for embedders only.** `AccountStore` has moved out of the library
into the command line, at `tariffkit.cli.AccountStore`. Nothing below the CLI
opens a file to find an account any more: it is handed the
`AccountProfile` it should price with.

| was | now |
| --- | --- |
| `from tariffkit.account import AccountStore` | `from tariffkit.cli import AccountStore` |
| `tariffkit.account.ProfileNotFoundError`, `ProfileStorageError`, `ProfileConflictError` | `tariffkit.cli.account_store.<same>` |
| `tariffkit.account.ProfileNameError` | gone; there are no names left to be invalid |
| `create_app(use_account=True, profile_repository=store)` | `create_app(profile=store.load())` |
| `MqttPublisher(engine, settings, store=store)` | `MqttPublisher(engine, settings, profile=store.load())` |

`AccountStore(base)` still takes an optional directory to keep its
`tariffkit/` folder under, which is what the tests use; with no argument it is
`$XDG_CONFIG_HOME/tariffkit` as before. The on-disk format has not changed, so
there is nothing to migrate.

Why: `tariffkit.web` and `tariffkit.mqtt` each reached into `~/.config` to load
an account, which made the file a hidden input to a library call -- two
processes given the same `Config` priced differently depending on what was on
the machine that imported them, and an embedder that already held a profile (as
Home Assistant does, in its config entry) still paid for the lookup. Where the
account lives, how it is locked, and who may read it are decisions an
application makes.

**Home Assistant is unaffected.** It has always been handed a profile from its
own config entry.

#### The audit harness no longer takes `--account`

`audit reconcile`, `audit run` and `audit doctor` accepted a profile name and
then ignored it: there is one account. Passing one now fails as an unknown
flag rather than silently auditing a different agreement from the one named.

#### Named credential sets are gone

`tariffkit.secrets.get_named_secret`, `set_named_secret`,
`delete_named_secret` and `configured_named_secrets` are removed. They existed
so two profiles could share one PG&E login without storing the password twice,
and nothing has called them since the profiles went: one account reads one set
of credentials, stored with `tariffkit credentials set pge.username`. The
integration's `sanitize_profile` goes with them -- it stripped a field the
model no longer has, so it claimed a protection it was not performing.

The documentation has been brought in line with all of this: `docs/accounts.md`
(now "Your account"), `billing.md`, `configuration.md`, `containers.md`,
`web.md`, `home-assistant.md`, `use-cases.md` and `audit/README.md` no longer
show `--account NAME`, a profile name argument, `[account] default_profile`, or
`--credential-set`.

`tariffkit --help` said the `account` command manages "named account profiles";
it manages your account's dated history. `tariffkit account update --json`
emitted its result under a `profile` key, now `account`.

#### `account show` and `account history` printed the same thing

`show` listed the epochs, which is what `history` is for -- with no statement
evidence recorded the two were identical output, and one of them was pointless.

`show` now answers "what is my account?": the settings in force today, fully
resolved, the date the epoch they came from took effect, and the meter entities.
`--json` gives `{effective, config, meter_sources, epochs, observations}` rather
than the whole file, which is what `account export` is for. An account whose
epochs are all future-dated says so instead of failing.

`history` is unchanged: every epoch, and the statements that established them.

#### `credentials list` printed nothing on a machine that is fully configured

It listed only the names stored in the OS keyring, so a machine keeping its
credentials in `~/.config/tariffkit/.env` -- which every source reads first --
got no output at all. Empty is indistinguishable from a broken command, an
uninstalled `keyring` extra, or a backend that is not being read, and it was
reported as exactly that doubt.

It now names the backend and every credential with the source it resolves
from -- `environment (PGE_USERNAME)`, `.env (HA_TOKEN)`, `keyring`, or
`not set` -- and still never prints a value. A machine with no usable keyring
(a headless container, or `TARIFFKIT_DISABLE_KEYRING=1`) says
`keyring: none available here` rather than leaving it to be inferred.

`tariffkit.secrets.SECRET_ENV` is the new mapping behind it, and
`keyring_backend()` reports the backend in use.

#### `tariffkit bill` fetches the Green Button export itself, and keeps it

It asked for a CSV path -- "give a Green Button CSV path, or use --source ha or
--source influx" -- on a tool that can download the file. Given `--start` and
`--end` and no path, it now takes the export from
`~/.cache/tariffkit/pge/green-button/`, downloading it from the portal only if
it is not there:

```
  source: Green Button, downloaded (2880 intervals, ~/.cache/tariffkit/pge/green-button/2026-07-29_2026-08-27.csv)
  source: Green Button, cached 2026-07-29..2026-08-27 (2880 intervals, ...)
```

The portal generates each export on demand -- a job, a poll loop, and a signed
URL, about twenty seconds -- and returns the same readings every time for a
range that has closed. A **wider file serves a narrower request**, since
readings outside a billing period are ignored when it is priced, so one
download of a year prices every cycle in it; the narrowest covering file wins.
`--refresh` downloads again, for a cycle still open. Files are mode `0600` under
a mode `0700` directory, beside the session cache: an export carries a name, a
service address, and every quarter hour of consumption.

Passing a CSV still works and still skips the portal entirely.
`audit --green-button` reads the same cache, which was re-downloading one export
per statement.

New in `tariffkit.sources`: `cached_green_button`, `cached_exports`,
`CachedExport`, and `read_green_button_export`.

#### `tariffkit bill` defaults to the cycle you are in

With no `--start`/`--end` it prices the billing cycle open right now, through
today -- what you owe so far, which was the one question the command could not
answer without first looking up when the cycle began. Passing one of the two
without the other is refused rather than half-guessed.

Where the boundary came from is printed, because it is not always known:

```
  cycle: 2026-08-28 to 2026-09-09, the boundary your statements print
  cycle: 2026-09-01 to 2026-09-09, a calendar month, which is a guess -- run
    'tariffkit account sync --apply' for real boundaries, or set [billing] cycle_start_day
```

Statements date it exactly, and cycles are contiguous, so the open one begins
the day after the last statement ended -- no waiting to be billed. `[billing]
cycle_start_day` in `config.toml` is the fallback meter-read day; with neither,
the calendar month is used and labelled a guess. PG&E reads on business days,
so a real account's cycles open on the 29th, the 30th, the 1st and the 3rd in
consecutive months, which is why a fixed day is only ever close.

The resolution itself is not new -- the Home Assistant integration has used it
for its cycle-to-date entities all along. It has moved from
`custom_components.tariffkit.energy` into `tariffkit.billing`
(`resolve_cycle`, `cycle_start`, `statement_periods`, `Cycle`,
`STALE_EVIDENCE`), where the CLI can reach it; the integration imports it from
there now. `Cycle.source` no longer carries prose, only the basis
(`statement`, `day_of_month`, `calendar_month`), leaving the wording to whoever
prints it.

The Green Button cache drops ranges a new download wholly contains, so billing
an open cycle daily leaves one file rather than one a day.

`tariffkit bill FILE --source ha` no longer fails an assertion. A CSV path
names its own window only for the source that reads a CSV; every other source
is asked for a period, and had been reaching `assert period is not None` --
a traceback on a user error, and an `AttributeError` under `python -O`.

#### The cycle boundary comes from PG&E, not from a guess

The portal knows exactly when every cycle it billed opened and closed, and will
say so: `WUE_GetUsageExportBills` is what fills the export widget's bill-period
dropdown. `tariffkit bill` with no dates now asks it, so the default window is
the utility's own boundary rather than a meter-read day or a calendar month:

```
  cycle: 2026-08-28 to 2026-09-09, the boundary PG&E billed on
```

No statement import needed and no PDF parsed. `bills` turns out to be a field on
the *account* rather than on `Query` -- which is why the operation name was no
guide to it and introspection (disabled here) could not be asked -- so it was
captured from the widget's own request; `audit/pge/PORTAL.md` has the query and
the shape of what it returns.

The list is cached at `~/.cache/tariffkit/pge/bill-periods.json` (mode `0600`)
and refreshed only when it stops covering the present, which is the same
staleness bound statement evidence uses and means the same thing: a bill has
been issued that this does not know about. So pricing from InfluxDB or Home
Assistant does not begin requiring portal credentials or a round trip. A refresh
that cannot happen keeps what is on disk -- an old boundary reported as old
beats a command that fails offline.

New in `tariffkit.sources`: `cached_bill_periods` and `read_bill_periods`.

#### An open cycle ends at the last published read

PG&E publishes interval reads a day behind, so pricing a cycle "through today"
fetched an export that stopped a day short and then reported the shortfall:

```
  warning: 2026-08-28..2026-09-09 (E-ELEC): readings cover 288.0h of the 312h period (24.0h missing)
```

Nothing was missing. The reads are not published yet, which is a fact about the
publishing schedule and not about the meter -- and the day was also charged a
Base Services Charge it should not have been. The window now ends where the
utility says its readings do, which the export widget has always done:

```
  cycle: 2026-08-28 to 2026-09-08, the boundary PG&E billed on
```

That answer comes from `WUE_GetUsageExportAvailableAMIReadsTimeInterval`, asked
once a day and kept in `~/.cache/tariffkit/pge/available-reads.json`. It is also
what makes the export cache work for an open cycle: yesterday's file covers
today's question, so the same cycle is not re-downloaded every morning. A
`tariffkit bill` that has already asked today costs no network at all.

New in `tariffkit.sources.pge`: `available_reads` and `cached_available_reads`.

#### The account carries the cycle boundaries it was billed on

**Profile schema 1 -> 2.** `AccountProfile` gains `billing_periods`: the cycles
the utility says it billed, inclusive at both ends, sorted and non-overlapping.
Schema 1 files -- every account file and every Home Assistant config entry
written so far -- are read unchanged and simply have none; saving writes 2.

Boundaries without the statements that print them is the point. Only the CLI
has portal credentials, and only a statement import previously produced exact
cycles, which meant parsing PDFs. `tariffkit account periods --apply` now reads
them from the portal and records them, and `account export` carries them
wherever the account goes -- above all into Home Assistant, which holds no
portal credentials by design and was otherwise left approximating with a
meter-read day. They can also be typed in by hand from the portal's own
bill-period dropdown; `docs/home-assistant.md` has that path.

`known_periods(profile)` is what every cycle lookup now reads. Where two
periods overlap the wider one is kept, because cycles tile rather than nest and
anything inside another period is a partial view of the same cycle. That is
usually the statement, which is one page -- a cycle whose service agreement
changed partway is one billing period on it and two entries in the portal's
list, and a cycle-to-date figure has to follow what was billed. It is
occasionally the portal, when a statement was only partly read and spans a few
days inside a cycle the portal has whole. The rule lives once, in
`tariffkit.billing.merge_periods`.

The label printed beside the window names the source that actually answered,
found by which period the cycle resolved from. Deciding it from "does the
account hold any statements" called a portal boundary a statement's on any
account holding one old PDF.

#### `charges_by_bucket` returns two values, not three

**Breaking, for embedders only.** Its third element was a bucket-to-unspent map
that has been all zeros since the in-cycle clamp was removed, threaded through
two call sites whose arithmetic it no longer changed. `offsettable,
non_offsettable = charges_by_bucket(bill)`.

#### A skipped statement never names the file the sync deleted

`account sync` reports a statement it could not read by the date the utility
issued it, stripping the source the parser prefixes its messages with -- which
for a sync is a loop index inside a cache directory the run removes. It stripped
everything up to the first `": "`, and four of the parser's messages separate
the source with a space, so those printed the temporary name anyway; a fifth
contains a later colon of its own and lost the half that said what went wrong,
keeping only its problem list. It strips the source *name* now.

#### A Home Assistant row that is absent is not a measured zero

`_readings_from` marked a direction unmetered when the recorder's figure was
refused, and not when the row was missing altogether -- so an hour where only
one entity reported read as "that direction moved nothing", and coverage
accepted it. The recorder compiles an hour for any entity that has a state, and
a flat counter still yields a change of zero, so no row at all means the entity
had no state. It is unknown now, like a refusal, and an interval with neither
direction known is dropped rather than counted as two zeros.

The test that would have caught it was asserting the wrong thing under the
wrong name: `test_a_backwards_counter_is_clamped_not_negated` passed because a
*refused* value reads as 0.0, on a series whose export half was absent. A
backwards counter is refused, not clamped, and the test says so now.

#### A partial hour keeps its five-minute rows when nothing replaces them

An hour the fine series only partly covers gives its rows up to the hourly row
that covers it -- but only if there is one. Surrendering them unconditionally
discarded every reading in a window that begins mid-hour, where no hourly row
can exist: an explicit `resolution="5minute"` request for 04:20-05:00 had eight
rows per entity on a real instance and raised "no statistics for ... between
...".

#### Coverage says what was reconstructed and what was merely shifted

They were one sentence, and it was wrong either way. Counting only the
intervals the source could not speak for reported "0 interval(s) covering 0.0h
and 4.5 kWh"; counting every interval that carried a share said 347 hours of a
768-hour cycle were "reconstructed across gaps" when all 347 were measured and
there were no gaps. A reconstructed interval and a shifted kilowatt-hour are
different claims and now get different sentences.

#### Home Assistant is the default reading source

`tariffkit bill` read Green Button unless told otherwise. It now reads the
account's own meter through Home Assistant, and a CSV path still selects the
Green Button reader because that is what a CSV is.

The utility's export is not always complete. On one real cycle it held 0.13 kWh
of the 71.6 the meter recorded -- PG&E truncated a 32-day request to its last
two days -- while the meter matched the printed statement to 0.00 kWh. The
account's own instrument is the safer thing to reach for first; the export
stays as the independent check, which is the job it does well.

#### The audit's time-of-use check fires above the noise, and nowhere else

**A line the rate table reproduces is still a mismatch.** For a while it was
not: a line was downgraded to a passing "metering" verdict whenever pricing it
from the statement's own kWh reproduced the printed amount. That figure is
`rate x printed kWh` and never passes through the billing engine, so it tests
the rate table and nothing else -- and a hundredfold error in how the engine
applies that rate leaves the two identical. Measured on a constructed
statement: a $1,152.36 error on a $295.30 bill reconciled clean and vanished
from the report. Reverted, and pinned by a test.

**The split is asserted, above the reference's own noise.** `kwh_ok` allowed
0.5% of the larger figure with the scale floored at 1 kWh -- an absolute
five-watt-hour test on any small quantity. Green Button rounds every interval
to two decimals (measured: all 2,880 values in a cycle's export carry exactly
two), so its quantisation accumulates about 0.31 kWh over a cycle at two sigma.
A 0.06 kWh peak difference worth a penny failed a solar cycle while a 0.65 kWh
one worth thirteen cents passed a winter cycle. A 0.35 kWh floor holds the test
above that noise, and it still asserts: a cycle's worth of misattributed peak
energy is real money.

The statement prints the split and would be a better arbiter than a second
derivation of the meter. Reading those rows is not solved -- an attempt counted
"Part Peak" as peak on the two schedules this account actually runs, took the
maximum of a cycle's two seasonal rows instead of their sum, and read exported
kilowatt-hours as imported -- so it is not in the tree. That is worth doing
properly, as its own change.

#### A cached export is named for what it holds

The portal does not always honour the range it is given: a 32-day request for
an older cycle came back with its last two days, and its own archive filename
said so. Naming the file for the *request* cached two days under a
thirty-two-day name, so every later lookup inside that span got a hit with
almost nothing in it, and the pruning would delete a correct narrower file for
overlapping a range this one only claimed to hold.

The span is read from the readings themselves, the file is named for it, and a
short answer is logged. An export with no readings at all is refused by the
period that was asked for rather than by "CSV contained no data rows" -- but
only a genuinely empty one: an unrecognised header, an unparseable timestamp
and a non-numeric quantity are the parser failing, and reporting those as "the
portal sent nothing" pointed the reader at PG&E for a regression of ours.

#### Three commands raised where they should have reported

`account source show` had asked for a `profile` key since the account became
singular, so it raised `KeyError` rather than printing the mapping. `bill
missing.csv` and `--config missing.toml` let `FileNotFoundError` through, where
every other misconfiguration is one line and exit 1.

#### A superseded period does not come back with a label

`tariffkit bill` labelled each boundary with the source it came from by
recording labels as the merges went along, which kept the periods the merges
had just discarded -- putting a partial statement back beside the whole cycle
that supersedes it, and handing the cycle lookup the partial span again. The
labels are read off the merged list.

#### `credentials set --set NAME` needs re-storing after upgrading

Named credential sets are gone, so secrets kept under one are no longer read:
the account adopts fine and then portal commands stop authenticating. The
"credentials not found" error says so, and what to do about it.

#### Copies of an account keep every field it has

`billing_periods` was dropped by six paths that rebuilt `AccountProfile` field
by field: Home Assistant's **Import profile** (so the documented export ->
import workflow lost every boundary on arrival), both of its epoch editors,
`account update`, `account source set`, and applying a reconciliation -- so
`account sync --apply` erased what `account periods --apply` had just recorded.
They all use `dataclasses.replace` now, which forwards what it is not told to
change, so the next field added cannot go the same way.

#### `{"profile": false}` means no, and so does `0`

The REST switch counted any non-null value as "price this from the account", so
`false` turned it on -- and, alongside a `config`, was then rejected for asking
for both. Special-casing the boolean left `0`, `""`, `[]` and `{}` still meaning
yes, which is the same bug with a different literal. Anything falsy is no; a
legacy profile name still says yes.

#### The fixed charge is prorated, and the documentation said it was not

`BillEngine` has priced the Base Services Charge day by day since before this
branch, specifically to match the utility's proration -- AB 205's charge began
mid-cycle and that cycle is billed 30 days at nothing and 2 at the new rate.
`docs/billing.md` listed the opposite as a known limit and `docs/use-cases.md`
repeated it as a trap, which would have had an embedder compensating for a
limitation that does not exist.

#### Smeared energy is counted whether or not it is flagged

The 0.01 kWh floor decided whether a reconstructed share *existed*, not just
whether its interval was worth flagging, so it could accumulate out of sight:
five hundred shares of nine watt-hours is 4.5 kWh time-shifted and no warning
at all. The magnitude is always recorded now and materiality is applied to the
cycle total, which is the question a reader is actually deciding. Every
interval that carried a share is counted in the warning, which otherwise read
"0 interval(s) covering 0.0h and 4.5 kWh" -- a sentence at war with itself.

#### Reading the account no longer writes to the configuration directory

Constructing `AccountStore` created and `chmod`-ed `$XDG_CONFIG_HOME/tariffkit`,
so merely asking whether an account exists was a write -- on every `now`,
`forecast`, `bill`, `mqtt` and `serve`, and even with `--config` naming a file
somewhere else entirely. On a read-only configuration mount, which is how
`docs/containers.md` says to run `serve`, the chmod failed at startup, and it
failed as a raw `PermissionError` traceback rather than as `error: ...` and
exit 1.

Nothing is created until something is written, `--config` short-circuits the
account lookup entirely, and a directory that cannot be created is reported.
The legacy-profile adoption path no longer leaves its temporary file behind
when the write fails partway.

**Reading no longer demands mode 0700 of the directory.** It only ever passed
that check because construction had just chmod-ed its way there, so making
creation lazy left the demand without its self-heal: a `~/.config/tariffkit`
made by the `mkdir -p` in `docs/accounts.md` is 0755 under a default umask,
and every command that so much as asks whether an account exists refused to
run -- `account init` included, leaving no way out. A read-only 0500 mount was
refused for being *more* private than asked. Reads accept the directory and
check the account file's own 0600, which is what protects it; writes create it
0700 and tighten it, that being the point at which doing so is ours to do.

#### Uncompensated pre-PTO exports are reported as a figure, not a warning

Net Billing begins at Permission To Operate, so the cycle containing it always
holds exports the tariff grants nothing for -- the arrangement starting, not a
defect. `Bill.warnings` means "something here may be wrong" and
`BankState.trustworthy` disqualifies a bank for any entry in it, so that one
note kept a real balance unspendable for its whole first year. The energy is
now `Bill.uncompensated_kwh` and is published as an entity attribute, and
counterfactual rate comparisons can price a pre-PTO month without filtering a
warning string to do it.

### Fixed
- **`tariffkit bill` uses your account profile without being asked.** With one
  profile it is simply yours; with several it now refuses rather than falling
  back to `config.toml`, because a bill priced from the wrong agreement is a
  plausible wrong number and not an error. That fallback had priced a CCA
  account as bundled without a word, giving it one export credit bank where it
  has two and pricing a cycle that crossed a rate change at a single tariff.
  `now` and `forecast` keep the fallback: what the price is this hour is a
  question about today, which is what a config describes.
- **`tariffkit bill` prints what a statement would charge.** It printed
  `Bill.total` under the word TOTAL, which subtracts every export credit from
  every charge -- something the tariff does not allow, since credits are scoped
  and credit beyond what its own bucket can absorb banks rather than reducing
  the bill. On an exporting account the two are nowhere near each other and only
  one appears on a statement: a 2026-07-29..08-27 cycle printed -73.36 where the
  statement's electric charges were 14.22, and nothing on the page was -73.36.
  The command now prints gross charges, credit applied and AMOUNT DUE; that
  cycle reads 14.20. `docs/use-cases.md` had been saying `Bill.total` was the
  wrong figure while the command printed it as the headline.
- **`tariffkit bill` prints each export credit bank, and never their sum.** One
  closing figure could say neither how the bank moved nor whose it was. It now
  prints the four columns a statement prints -- opening, earned, applied,
  remaining -- one row per supplier, because on a CCA account there are two
  banks on unrelated settlement calendars and adding them gives a number no
  statement shows. A cycle that announced a single "+94.43" now reads PG&E
  17.74/5.83/11.92 against a printed 17.78/5.82/11.96, and MCE 86.42 earned and
  0.00 applied against a printed 86.16 and 0.00.
- **The PCIA is an Energy Delivered charge.** `SCOPING_VERIFIED` named the
  evidence it needed -- a cycle whose credits exceed the charges they may offset
  -- and the 2026-09-03 statement supplies it: PG&E applied $2.94 of Energy
  Export Credit where the time-of-use delivery rows come to $2.54, and the only
  charge that closes the difference is the PCIA at $0.40. It had been in the
  bonus bucket, reachable by the ACC Plus adder and nothing else, so delivery
  credit stopped forty cents short every cycle and banked what it should have
  spent. Applied delivery credit now matches the printed $2.94 exactly.
- **An in-cycle offset larger than its charges is credited, not banked.** The
  2026-09-03 statement settles a question `SCOPING_VERIFIED` has been open on:
  MCE's Solar Bonus Credit of -8.33 against smaller generation charges printed
  "Total MCE Electric Generation Charges  -$6.85", and that -6.85 goes straight
  into the summary, helping print a -21.96 credit balance. MCE's own bank on the
  same page shows where it did *not* go -- beginning 12.63, earned 86.16,
  applied 0.00, remaining 98.79, closing exactly with no room for a remainder.
  The overrun was being banked instead, overstating the bank by $7.13 on that
  cycle and understating the credit the customer was given. Every cycle
  reconciled before this one had generation charges larger than the offset, so
  the case never arose.
- **The smeared-gap warning reports what was actually spread.** It summed the
  whole energy of every interval a wide sample gap merely *touched*, so a cycle
  with 0.83 kWh genuinely spread across gaps was reported as having 144.6 kWh of
  guessed time-of-use split -- 172 times over, on a figure whose only job is to
  say how much to distrust. `IntervalReading.smeared` carries the magnitude, and
  an interval is flagged on the energy it took from a gap rather than on a gap
  having passed through it: most wide gaps here are the counter standing still
  overnight, which guesses nothing. Below ten watt-hours nothing is flagged at
  all, that being half a cent at the widest export rate spread on this tariff.
  The same cycle now reports 15 intervals and 0.4 kWh.
- **The two readers of Home Assistant statistics are one reader.** The
  integration reads them through the recorder in process and the library reads
  them through the WebSocket API, and the derivation had been written twice --
  so the counter repair below existed in one and not the other, and
  `tariffkit bill --source ha` kept dropping intervals the integration had
  learned to recover. `interval_energy` and `carry` live in
  `tariffkit.sources.homeassistant` now and both readers call them, and the
  library asks the recorder for `state` alongside `change` so it can.
- **An hour the fine series only partly covers no longer loses the rest of
  itself.** `resolution="auto"` dropped the hourly row for any hour holding
  *some* five-minute data, so an hour the fine series resumed inside left its
  earlier part in neither series: on a real cycle the five-minute statistics
  resumed at 04:20 after a restart and 04:00-04:20 went missing, reported as a
  gap. An hour is now only replaced by fine readings that cover it in full.
- **Meter data is declared as netted where it is read.** `BillEngine.compute`
  and `compute_segments` take `netted`, and the CLI, the audit harness and the
  integration all pass it: every source shipped here reads a meter's own import
  and export registers, which legitimately carry both directions in one interval
  once aggregated to an hour. Only the integration's own coverage check knew
  that, so every bill priced anywhere else reported hundreds of intervals as
  suspect on every solar cycle -- noise that never cleared.
- **A counter reset the recorder only believed in no longer costs the hour.**
  A `total_increasing` sensor reading 0.0 is taken for a counter reset, so the
  recorder reports the whole counter as the next hour's `change` -- 1455 kWh on
  a meter that had moved 0.42. A smart-meter reader does this several times a
  day while it re-establishes its session with the meter. Refusing that figure was
  right and dropping the hour with it was not: the counter itself is in `state`,
  and differencing it against the previous hour brings the energy back. On the
  account this came from, a cycle credited 54.206 kWh against the filtered
  sensor's 67.016 and now reads 66.938 -- a fifth of its exports, recovered from
  data the integration already had. The repair refuses where it would be
  guessing: a spurious zero has nothing to difference, and a gap in the series
  means the counter also advanced through unrecorded hours, so crediting that to
  the hour the series resumes would price days of energy at one hour's
  time-of-use rate. `tariffkit.sources.influx.monotonic` is the same rule one
  layer down, on raw samples rather than hourly rows.
- **One unreadable statement no longer discards a whole sync.** A
  `StatementError` from any single PDF propagated out of the loop in
  `account sync` and `account import-statement`, so an account with years of
  statements imported none of them because the newest one would not parse --
  and the command exited non-zero, as though the portal or the credentials were
  at fault. Reported from a real sync as
  `error: statement-0000.pdf: no total amount due found`, naming a temporary
  file inside a cache directory the same function deletes on its way out:
  nothing the owner could open, and no indication of which statement it meant.
  Statements that cannot be read are now skipped, reported on stderr as
  `skipped <date>: <reason>`, and returned in a `skipped` list beside the
  proposals -- named by the date the utility issued the statement rather than
  by the loop index.
- **A statement for an account in credit is read, not refused.** When the
  balance is negative the utility prints "CREDIT BALANCE - NO PAYMENT DUE" and
  the figure instead of a "Total Amount Due" line, so the parser refused the
  statement with `no total amount due found` -- true, and not an error. The
  credit balance is now the statement's `amount_due`, negative, which is what
  `self_check` already expected: on 2026-09-04 the detail sections and summary
  adjustments close on it to the cent (21.07 - 6.85 - 36.18 = -21.96).
- **Kilowatt-hours were billed as dollars when a rate printed tight against its
  "@".** `_fields` separates columns on two or more spaces, so whether
  "@ $0.10867" arrives as one field or two depends on how wide that gap came
  out. Where it came out as one, the row fell past every "@"-aware branch to the
  fallback scan -- which takes the first money-shaped field, and a metered
  quantity prints as "10.122000", which `MONEY` accepts. An Off Peak row was
  billed at $10.12 instead of $1.10, and one statement's delivery section summed
  to 51.67 against a printed 21.07. It now sums to 21.07 and every metered row
  carries its quantity and rate.
- **Two rows sharing a truncated label are no longer read as overlapping
  sections.** Recognition widens the gaps inside a label and `_fields` splits on
  two spaces, so "Current PG&E Electric Monthly Charges" and "Current Gas
  Charges" both come back labelled "Current" on a combined statement. The
  duplicate check keyed on the label alone and refused the statement. What it
  looks for is one row collected twice, and such a row carries the same amount
  both times, so the amount is part of the key now.
- **A failed recognition names the reading that came closest.** When no reading
  checked out, the error reported whichever reading raised -- so a statement
  blamed "page 3 prints an unsupported tariff", from a reading whose "p.m." had
  vanished, while the reading that named the tariff correctly had failed its
  self-check for an unrelated reason. A reading that produced a whole statement
  and came up short by a row is the closer near-miss and its problems say what
  to look at, so that is what is reported.
- **The gas half of a Climate Credit is read.** PG&E issues the California
  Climate Credit against gas and electricity separately, in April and October,
  and prints both in the summary. The electric half was read by name and the gas
  half by nothing, so a combined April statement failed its own check by exactly
  that credit -- 135.21 electric, -58.23 electric adjustments, 65.71 generation,
  62.22 gas and -67.03 unread, against a printed 137.88 the five of them reach
  precisely. `Statement.gas_adjustments` carries it, and `electric_charges` and
  `self_check` both account for it.
- **A tariff is recognised from the words when the meridiem is lost too.**
  Recognition returned "(Peak Pricing 4 9    Every Day)" -- dash and "p.m."
  alike swallowed by the gaps they sit in -- so no tariff matched and the
  statement was refused as printing an unsupported one. "Peak Pricing 4 ... 9"
  is anchor enough on its own.
- **A dropped hyphen between the peak hours no longer costs a statement.**
  Recognition loses the mark -- it is small and it sits in a gap, the same
  reason `_implied_at` exists for the "@" -- so "Peak Pricing 4 - 9 p.m." came
  back as "4 9 p.m.", matched no tariff, and `_agreements` refused the whole
  statement as printing an unsupported one. The dash is optional now, made safe
  by anchoring on the "p.m." after the hours.
- **A cycle split at a rate change is one agreement, not two.** The utility
  splits a cycle where a rate change or the June 1 season boundary lands and
  prints both blocks under one schedule -- 08/28-08/31 then 09/01-09/28, one
  Time-of-Use agreement. `_agreements` read that as two agreements for one
  schedule, called it ambiguous, and refused the statement whole. Spans that
  continue one another now join; spans with a gap between them stay two, which
  is the case the check exists for.
- **One period printed twice is one agreement.** `_agreement_spans` deduplicated
  on the dates *and* the day count, so a span printed twice with two different
  counts came back as two agreements and `_agreements` refused the statement
  with "prints 2 date spans for one delivery schedule". The day count is
  evidence about a span, not part of its identity.
- **An impossible date from recognition no longer escapes as a `ValueError`.**
  `read_statement` discards a reading that raises `StatementError` and tries the
  next, which is how the recognised Type 3 statements are read at all -- but
  `_parse_date` raised `ValueError`, so a misread digit (`41/12/2026`) escaped
  that guard and ended the process with a traceback from inside the loop whose
  purpose is to survive it.
- **A bank the money entities would not spend, and would not say so.** The
  amount-due entities refuse an export credit bank they cannot vouch for, which
  is the safe direction, but the note explaining the refusal was gated behind
  `bank_pending` -- a field only ever set when the fold produces *no* bank. A
  fold that succeeded and warned was therefore dropped in silence: a $26.55
  cycle stated before a $7.73 delivery balance, `warnings` empty and
  `quality.complete` true, while the bank entity beside it printed nine
  warnings. `bank_pending` now explains only an absent bank; a bank that exists
  is judged on its own.
- **One meter's counter restart no longer discards the other meter's energy.**
  The Home Assistant statistics source built one reading from both directions
  and refused the whole interval when either exceeded the plausibility ceiling.
  Import and export are separate entities whose running sums restart
  independently, so a bad export series destroyed good import data: against an
  unfiltered meter counter that resets its session several times a day, 56
  refused hours took 21.4 kWh of import with them and billed 74.5 kWh as 53.1.
  Each direction is now judged on its own, and only an interval with no usable
  half is dropped. A refused direction reads as `0.0`, because a float cannot
  say "unknown", so the interval records it in `IntervalReading.unmetered` and
  `check_coverage` reports it -- keeping the good half's energy *and* the hole,
  where dropping the interval kept only the hole.
- **Two tests that had stopped running.** `test_a_running_sum_restart_is_discarded`
  -- the one guarding the plausibility ceiling on meter readings -- was
  uncollectable: it used `caplog`, which `pytest_homeassistant_custom_component`
  overrides by requesting, and pytest 9 reads a plugin fixture asking for its own
  name as a recursive dependency. A `captured_logs` fixture replaces it, formats
  each record through the logging machinery, and does not collide with the
  plugin. A suite reporting an error beside its passes still looks green at a
  glance, which is how this survived.

## [0.7.0] - 2026-09-07

### Added
- **A two-day forecast curve on every component-group entity**: each band --
  `import_generation`, `import_distribution`, `export_delivery`, and the rest --
  now carries its own `raw_today` / `raw_tomorrow` list, in the same shape and
  the same 30-minute Pacific-day slots as the price entities' Predbat
  attributes. Stacking a direction's bands reproduces its price curve, so a
  dashboard can draw whichever split it means -- generation against the rest,
  PG&E's Delivery line (`distribution + transmission + surcharges`) against
  generation, or every band at once -- without the payload having named one for
  it. Published on the matching MQTT topics too, one band per topic, which keeps
  each payload inside Home Assistant's 16 KiB recorder ceiling; an
  MQTT-discovered sensor cannot mark an attribute unrecorded. Unlike the Predbat
  attributes these are not gated on Predbat mode, since charting what a price is
  made of has nothing to do with Predbat.

### Fixed
- **ACC Plus PDF parsing**: `pypdf` extraction of the ACC Plus table injected newlines inside figures. The values are now successfully rejoined and accumulated, fixing the failure to vendor ACC Plus rates.
- **NSC verification**: Validating a newly discovered True-Up month mistakenly verified against the previous vendored rate rather than the newly parsed rates, which always caused validation to fail. Data parsing is now successfully validated.
- **Dependency audit**: Prolonged acceptance of cryptography vulnerability through October to resolve the expired dependency audit.

## [0.6.1] - 2026-08-30

### Fixed
- The Amount Due breakdown adds up. `energy_charges + taxes + fixed_charges`
  overshot `gross_charges` by whatever the statement spent inside the cycle
  instead of banking -- a CCA's Solar Bonus Credit reduces that cycle's
  generation charges directly rather than entering the bank, so it reached
  neither `credit_applied` nor `bank_change`. `export_credits` did carry it,
  as it carries every export component, but nothing published how that total
  split between what banked and what was spent on the spot. So a consumer
  rendering the statement could show the components or show a total that
  reconciles, but not both, and could not tell the shortfall from a rounding
  error. The split is published as `in_cycle_offsets`, on the `amount_due_*`
  entities and in the backfill summary's cycles. It is a share of
  `export_credits` rather than a figure to add to it, and it is the part
  actually spent: an offset larger than the charges its bucket holds takes them
  to zero and banks the rest, which `bank_change` already reported.
- MCE's low-income export bonus is charted in the credits band rather than
  "other". `cca_care_fera_bonus` was added to the ledger's bucket map when the
  bonus started reaching bills, but not to the component-to-group table, so
  every CARE or FERA account on a CCA card drew $0.05/kWh in the export chart's
  safety-valve band. The price itself was right -- an ungrouped component
  still counts toward the total -- so only the breakdown was wrong. The
  invariant that nothing real lands in `other` was already tested, but over a
  matrix holding a bundled CARE account and an undiscounted CCA one and not
  the pairing that emits the component; it now holds that case too.

## [0.6.0] - 2026-08-30

### Changed
- The ACC Plus bonus credit now offsets the non-bypassable charges, which is
  what Schedule NBT says three separate times -- Special Condition 2.f names
  the four NBCs and adds "except for the ACC Plus credit", and 2.d and sheet 19
  say the same in their own words. They were modelled as reachable by nothing,
  so a bonus bank left them standing as cash owed. Ordinary export credits
  still cannot reach them. `energy_cost_recovery` is no longer counted among
  them; the tariff names four and it is not one. Accounts whose bonus bank
  exceeded their other charges will see a lower amount due; every reconciled
  statement is unaffected, because on those the bonus was smaller than the
  charges it could already reach.

### Fixed
- A CARE or FERA account on MCE is credited the low-income export bonus from
  the date its tariff starts paying it. The terms were vendored only on the
  April 2026 rate card, so exports between the Solar Billing Plan tariff's
  2023-12-01 effective date and 2026-03-31 still received nothing. They are
  dated by that tariff rather than by the rate card, and are vendored from it.
- Six weekday evenings in 2044 and 2045 are priced at peak again. Every export
  vintage covering those years duplicates Memorial Day, Independence Day and
  Labor Day onto the following day, and because no two of them disagreed the
  intersection that removes the artifact everywhere else preserved it -- so
  both years carried eleven holidays instead of eight, and E-TOU-D, whose peak
  applies on weekdays only, priced those evenings as off-peak.
- MQTT publishes at QoS 1 and reports a refusal. Everything is retained, so a
  dropped message is not a gap: the broker keeps serving the previous hour's
  price and the last will does not fire on a clean disconnect, so subscribers
  saw a stale price presented as current. A one-shot run could also publish
  before the broker acknowledged the connection, dropping every message and
  exiting successfully; it waits for the acknowledgement now.
- A bill history that cannot be parsed is reported as a failure rather than as
  an account with no bills. Three decode paths returned an empty list, and the
  CLI printed "received 0 statement update(s)" and exited successfully, so a
  portal change looked like a completed sync.
- A statement's recorded source no longer resolves against the working
  directory. It holds a basename, so hashing it picked up whatever file of that
  name was in the caller's directory -- binding one statement's facts to
  another document's digest, which either blocks a legitimate import as a
  conflict or records provenance for a file nobody read.
- Changing supplier or schedule through the options flow is validated. Only the
  setup flow checked the choice against the CCA's rate card, so a schedule the
  card does not cover was accepted through Configure, written to the entry, and
  left the reload failing -- every entity unavailable, with a log line as the
  only explanation. Both option branches now surface the same in-form error
  setup does.
- The annual cash-out reverses at the rate MCE's tariff names. Its Solar
  Billing Plan tariff says "the initial export credit will be reversed at the
  average Energy Export Credit (including Solar Bonus Credit) rate", and the
  function's own docstring quoted that line while asserting the bonus was
  already inside the figure it averaged. It was not -- the Solar Bonus Credit
  is spent against the cycle's charges rather than banked, so nothing reading
  earned credits could see it, and a cycle earning $5.50 averaged as $5.00.
  The reversal came out too small and paid out surplus the tariff treats as
  already covered.
- A run crossing two settlements that end on the same cycle reports both. They
  were de-duplicated by date alone, and because the sort puts the CCA cash-out
  first it was always the utility's event that disappeared from the reported
  settlements. No money moved either way; the attribute simply under-reported.
- A CARE or FERA account on a CCA that pays a low-income export bonus is
  credited it. MCE's Solar Billing Plan tariff pays "$0.05/kWh generation
  export bonus credit on all exports until December 31, 2028" -- more per kWh
  than the ACC Plus adder -- and the rate was vendored but read by nothing, so
  it reached no bill and the export price still reported itself complete.
- The Conservation Incentive Adjustment is offsettable by delivery credits. It
  is the distribution line the tariff implements the baseline credit with, and
  plain distribution was already treated that way, but it was absent from the
  bucket map and so fell to the non-offsettable default.
- An interconnection year inside NBT's locked window whose vintage is not
  vendored is refused rather than floating. Schedule NBT grants a nine-year
  lock for applications "no later than December 31, 2027", so answering 2027
  with the floating vintage priced it against the wrong values while still
  resolving that year's ACC Plus row. A year outside the window floats at
  either end, which is the tariff's own rule: "customers enrolling on NBT after
  its initial five years of availability ... will instead be compensated at the
  average hourly avoided cost values".
- An interconnection after the ACC Plus table ends earns no adder rather than
  raising. Schedule NBT makes the adder available to customers interconnecting
  "during the first five years of the tariff", decreasing "until the adder
  reaches zero"; the adopted table runs 2023 to 2027, so 2028 onward is zero.
- A CCA profile carrying both `rate_card` and `export_generation_rate` prices
  from the card. The explicit rate used to win, and that branch emits neither
  the card's solar bonus nor its ACC Plus adder -- a 22% under-credit into the
  CCA's bank, with the export price still reporting itself complete.
- MCE's Deep Green premium is priced at the rate the card published. It moved
  from $0.01 to $0.0125/kWh and only the 2023 and 2026 cards were vendored, so
  a Deep Green account was credited the older premium until 2026-04-01. Light
  Green generation was never affected: MCE did not reprice residential
  generation between those cards, which its own March 2025 board packet states
  and the intervening cards confirm rate for rate.
- The rate-card reader no longer drops a schedule whose card shares a header
  row. From MCE's December 2023 print onward the row reads "ETOUC, EMTOUC -
  Default Residential Time-of-Use", and a pattern anchored on one code before
  the dash matched nothing -- so E-TOU-C was dropped from the card, and because
  an unmatched line is not a schedule it was not reported as skipped either.
  Any regeneration from a current MCE card would have written a clean-looking
  file with a whole schedule missing.
- A CARE or FERA baseline credit is discounted like the charges it offsets. It
  was read straight from the rate sheet and applied at full value while every
  charge around it was scaled, so a discounted bill was met by an undiscounted
  credit: a 250 kWh within-baseline E-TOU-C January came to $46.29 where the
  same figures reconcile at $54.66, 18% of the bill.
- A CCA account's CARE or FERA discount is calculated on bundled-equivalent
  charges. Both sheets say so in identical words -- "the discount will be
  calculated for direct access and community choice aggregation customers based
  on the total charges as if they were subject to bundled service rates" -- and
  the CCA stack was being discounted instead, making the base several cents per
  kWh too high and the credit correspondingly too large. D-MEDICAL already
  rebuilt the bundled base; the two agree now.
- FERA is priced from Schedule E-FERA rather than a hardcoded 18% with no
  exemptions. The sheet exempts the Wildfire Hardening Charge, Recovery Bond
  Charge and Recovery Bond Credit before the discount is applied -- three
  components, where D-CARE exempts those and the Wildfire Fund Charge -- so a
  FERA discount was taken over a base that wrongly included all three, and was
  too large on every FERA bill. The rate and the exemptions are now vendored
  and regenerated like D-CARE's. A FERA bill dated before the sheet's
  2026-03-01 effective date now refuses rather than guessing at an earlier
  exemption list, which is how the schedules with one vintage already behave.
- Re-running a backfill no longer inflates the published history permanently.
  Every day in the window is written, including the ones that could not be
  priced, but the running total was anchored at the first day that *was*
  priced. When a rerun refused a day that a previous run had published -- a
  counter's catch-up across an outage is enough -- the base already held that
  day's old figure, and it was added again beneath a row reading zero. The day
  went on charging what it used to, and every later day carried it. External
  statistics are never deleted, so no rerun over the same window undid it. The
  total is now anchored at the first row actually written.
- A 29 February interconnection no longer breaks export pricing outright. The
  nine-year rate lock is measured to the PTO anniversary, which does not exist
  in the common year nine years after a leap year, so `lock_end` raised -- and
  `is_locked` runs on every export price, so such an account could not price a
  single exported kWh, fold a bank, or populate its rate-lock sensor. It falls
  back to the 28th, which is what the annual true-up already did.
- A CARE or FERA account is billed the Base Services Charge tier its programme
  is assigned, rather than the undiscounted one. D-CARE assigns CARE customers
  to tier 1 and E-FERA assigns FERA customers to tier 2, but the tier defaulted
  to 3 and nothing connected the two settings -- so a CARE account that simply
  never mentioned a tier paid $0.79343/day on E-ELEC instead of $0.19713, about
  $18 a month. The tier is now derived from the discount unless set explicitly,
  and an explicit tier that contradicts the programme is refused.
- Setting up a CCA account no longer reads its rate card on the event loop.
  Choosing a CCA validates the pick against the vendored card, which scandirs
  the provider's directory and parses TOML -- on the event loop, so Home
  Assistant's blocking-call detector logged three warnings for every submission
  of the step, each one telling the owner to open a bug report against
  TariffKit. The flow itself was correct and the account it produced was
  correct; only the thread was wrong. The read moves to the executor.

### Security
- The cached PG&E session cookie keeps its 0600 permissions, and no longer
  lands wherever the shell happened to be. `os.open`'s mode argument applies
  only when it creates the file, so an existing 0644 -- from an older version,
  a restore, another tool -- was rewritten world-readable despite the comment
  promising otherwise; `fchmod` now enforces it, as the profile repository
  already did. The default path was `.cache/pge/cookies.json`, relative to the
  working directory and described as "already gitignored", which held for this
  repository and nowhere else. It resolves under `XDG_CACHE_HOME` now, in a
  0700 directory.
- Home Assistant, InfluxDB and MQTT credentials are kept out of tracebacks.
  Their settings objects rendered a long-lived token or password in the default
  dataclass `repr`, which any frame-rendering traceback prints -- pytest, rich,
  a pasted issue report. `PgeSettings` had marked its own `repr=False` for this
  reason; its three siblings had not.

## [0.5.0] - 2026-08-29

### Added
- The Amount Due entities and the backfill response publish the terms their
  breakdown rests on. `gross_charges` is the ledger's own charge total,
  `non_offsettable` the part of it no credit may reach, and `not_paid_out` the
  clamp a statement applies rather than refunding. Together
  `gross_charges - credit_applied + not_paid_out` is the state exactly, for a
  day and for a cycle.

  The published components could not be added into the state before this. A
  component the statement spends inside the cycle rather than banking -- MCE's
  Solar Bonus Credit is the one vendored -- is subtracted from the charges
  before credits are applied and appeared in no attribute: not in
  `export_credits`, not in `credit_applied`. A consumer summing what was there
  landed short by exactly that, with no way to tell a missing term from a
  rounding error.

### Fixed
- The backfill coverage check no longer reports an hour it refused as an hour
  the recorder lost. An hour falls outside the priced set for three reasons and
  only one of them is absence: no statistics row, no usable total on the row,
  or a change refused as a counter's catch-up across an outage. All three
  printed as "is missing N of M hour(s)", which reads as data loss -- on a
  window whose 1392 hours were all on disk it claimed 299 were gone. What the
  recorder held and what could be priced are now counted apart and reported in
  their own words.

## [0.4.1] - 2026-08-27

### Fixed
- A CCA that credits an ACC Plus adder of its own is now paid for it. The adder
  is credited **twice** on such an account, once by each party at the same
  $/kWh, and only the utility's half was modelled -- so `export_price.total`
  understated a CCA export by the adder, and the CCA's credit bank was short by
  its entire balance.

  The two halves do not behave alike, which is why they are now separate
  buckets rather than one. The utility earns its half and spends it against
  delivery charges in the same cycle: on the 2026-08-04 statement PG&E's bank
  prints Bonus Credits earned $1.71, applied -$1.71, remaining $0.00. MCE earns
  its own $1.70 in that cycle and applies none of it -- its "Energy Export Bonus
  Credits Applied" line prints $0.00 -- so its EEBC balance reaches $3.29, being
  June's $1.59 plus July's $1.70 with nothing taken out. Folding both into one
  bonus bucket let the utility's spending drain a balance the statement shows
  growing, and reported the CCA's bank $3.29 light on a $12.63 balance.

  A new `cca_acc_plus` export component carries it, `CreditBucket.CCA_BONUS`
  banks it, and it is spent only after the CCA's export credit is exhausted --
  the order the statement's own applied figures imply. Providers declare it
  with `credits_acc_plus` in their rate card, defaulting to false, so no
  provider is credited an adder no statement has shown them paying. Only MCE's
  card sets it.

  No reconciled bill caught this and none could: the audit compares printed
  charges against computed ones, and a credit that is never applied never
  reaches a charge. It was found by folding a real meter series against the
  statement's printed balances instead of its charges.

  The audit map keeps up with it: the earned component is declared as one the
  statement does not print separately, and the CCA's grouped "Energy Export
  Credits Applied" rule now sums the bonus applied alongside the export credit
  applied, as the two printed lines it already reads do. Both were needed for
  `audit reconcile` to keep passing -- without the first it reported an unmapped
  component on every MCE cycle, and without the second it would have reported a
  mismatch on the first correct bill that spends the bonus.

  `month_curve` plots the export price again. It had been the delivery
  component plus the ACC Plus adders on a CCA account, omitting the CCA's
  generation credit and its bonuses -- a figure neither party pays. It and
  `price_at` are now built from one component rule instead of two copies of it.

  **Export prices change for CCA accounts**, by the ACC Plus rate --
  $0.00880/kWh for a 2026 residential interconnection. Forecasts, the MQTT
  payloads, and the EMHASS and Predbat attributes all carry the higher figure,
  because it is what the two statements between them actually credit.

## [0.4.0] - 2026-08-26

### Added
- `tariffkit.billing.run_lifetime` folds a run of bills from end to end,
  applying each annual settlement in the order it falls and returning a
  `LifetimeLedger` that reports what every cycle opened with. `run_ledger`
  carries a bank between cycles but knows nothing about the year closing on it,
  and `run_true_ups` computes each event independently of the others -- so
  composing them naively lets a second settlement be derived from a ledger that
  never saw the first one's clawback. This sequencing had grown inside the Home
  Assistant integration, where it was neither tested against tariff text nor
  visible to anyone reading the library.

- `Bill.import_charges` (energy charges plus the statutory taxes beside them),
  `Bill.marginal_buckets` (a span's time-of-use split, from two cycle-to-date
  bills), `CreditBalances.held_by` (which buckets each settling party holds),
  `AccountProfile.pto_date` (the earliest Permission To Operate any epoch
  records), and `TrueUp.settles`. Each replaces a copy that had accumulated in
  the Home Assistant integration; `held_by`'s copy had come to disagree with
  `tariffkit.billing.trueup` about who owns the generation bucket.

- `check_coverage` accepts `netted=True` for readings that come from a meter's
  own import and export registers, and `through=<moment>` for a period still
  running. Callers who knew these facts were filtering the function's messages
  by their text, which stops filtering the moment a new warning is added -- as
  one was.

  `through` also closes a hole that predates the flag. A missing hour at the
  *end* of a series is not a gap between readings -- a gap needs a reading on
  each side, and the whole point of a series that has stopped is that there is
  nothing on the far side -- so only a clock can tell an hour that arrived
  empty from one that has not arrived. Without one, a meter that stopped
  reporting went on producing a smaller figure that still called itself
  complete, indefinitely. `check_coverage` now measures the shortfall against
  elapsed time and names a series that has stopped, and the Home Assistant
  entities pass their clock so the running totals get both.

- The Home Assistant integration can optionally track what the meter actually
  moved. Name the cumulative grid-import and grid-export kWh entities under
  **Configure → Metered energy** — deliberately not part of initial setup,
  since pricing needs no meter and the counters are usually integrated after
  the tariff — and it adds running Energy Cost, Export Credit, and Amount Due
  entities for today and for the billing cycle to date, alongside Grid Import
  and Grid Export totals.

  Amount Due is what a statement would charge, not what a bill sums to. Under
  Net Billing a cycle earning more credit than it owes does not produce a
  refund: the excess banks, and a credit may only offset charges the tariff
  lets it reach, so Non-Bypassable Charges stay due however large the bank. The
  figure comes from `tariffkit.billing.apply_credits`; `credit_applied` and
  `bank_change` attributes say where the difference went. Reading `Bill.total`
  instead went negative on a heavily exporting cycle, which no statement does.

  Where the bank cannot be trusted -- not folded yet, unreadable, or folded
  across a gap -- the figures are stated before any bank offsets them, the
  reason appears in `warnings`, and `quality.complete` is false. Silently using
  a doubtful balance halved a cycle's charge while reporting itself complete.

  The counters do not have to reset daily: each hour's energy comes from the
  recorder's own long-term statistics, which already absorb counter restarts
  and reload gaps, with the hour in progress read live off entity state.
  Readings are priced by `tariffkit.billing.BillEngine`, the same code that
  reconciles a printed statement, so the running figures carry time-of-use
  bucketing, the Energy Commission Tax, the baseline credit, the whole day's
  Base Services Charge, and the rule that exports before Permission To Operate
  earn nothing.

  Today's figures are the cycle's movement across today rather than a one-day
  bill, because parts of a bill are cumulative over a cycle rather than
  additive over its days -- the baseline allowance is granted per cycle and
  consumed in day order, so pricing a day alone grants it one day's allowance
  however much the cycle had banked. Gaps, overlaps and reconstructed intervals
  in the metered series are reported in `warnings` and clear
  `quality.complete`, so a recorder outage understates the figure loudly rather
  than quietly.

  A **Billing cycle start day** setting names the day of the month the meter is
  read, but it is only a fallback: where the profile carries imported
  statements the cycle boundary comes from the statements themselves, which is
  the only way to match a real bill -- PG&E reads on business days, so
  consecutive cycles open on the 29th, the 30th, the 1st and the 3rd. A
  `cycle_boundary` attribute reports which was used.

  Naming no entities creates none of these, leaving every existing entity
  byte-identical. One exception is worth knowing: an account profile imported
  from the CLI carries its own `meter_sources.ha` mapping, and that mapping is
  honoured, so such an entry gains the entities without anyone opening the
  form.

- A `tariffkit.backfill_usage` action prices metered history into long-term
  statistics, so cost and credit for days before the meters were configured
  appear in Home Assistant rather than only through the CLI. It writes external
  statistics under a `tariffkit:` namespace -- the shape `opower` uses for
  utility history -- which leaves the running entities' own series and the
  recorder's compilation of them untouched. One row per finished day, each being
  its cycle's movement across that day, so the days sum to what their cycle
  actually charged. The run is folded through every annual settlement it
  crosses, not merely carried forward, because a true-up claws back credit
  already paid out as Net Surplus Compensation -- so a cycle after an
  anniversary opens with less bank than a straight fold would give it, which is
  what the live entities have always done.
  It defaults to the billing cycle containing the PTO date -- where bills begin
  meaning anything, since Net Billing compensation runs from Permission To
  Operate -- and reports a per-cycle bill alongside the daily rows, which is
  what an export credit ledger folds.
  Rerunning replaces the window rather than appending to it, which is how a
  corrected account history is picked up; a rerun over a later window continues
  the running total it finds rather than restarting it. Only days the recorder
  actually holds readings for are priced, and coverage is judged per meter, so
  one direction cannot vouch for the other.

- **Export credit bank** entities carry the Net Billing credit balance
  between cycles, which no entity computing forward from the day meters were
  configured can know. It folds every closed cycle since the one containing the
  PTO date -- so it opens at zero by construction, with no balance anyone has to
  supply -- applying each cycle's credits against its charges through
  `tariffkit.billing.run_ledger`, and continuing from a true-up's own closing
  balance where the run crosses one. It refuses rather than reports where the
  run has a gap: the library's ledger deliberately does not check for one, and a
  bank folded across a missing cycle reports a balance that never existed. The
  balance is recomputed when a cycle closes rather than accumulated, so
  correcting account history fixes it instead of leaving a stored figure quietly
  wrong. There are two entities because a Community Choice Aggregator account
  has two banks -- the utility's delivery and bonus credits against the CCA's
  export credit -- settling on unrelated calendars, which a single total would
  merge into a figure no statement prints. Every annual settlement in a run is
  applied in order rather than only the most recent, since each is computed
  independently and a later one cannot see an earlier one's clawback. A
  settlement that reverses and pays nothing -- the utility's, on a CCA account
  -- is recorded without consuming cycles, so it cannot shorten the other
  supplier's cash-out year and leave unreversed credit in the bank.

### Changed
- **Only affects development checkouts.** The metered-energy entities below are
  new in this release, so no published version ever carried their earlier
  names; this note is for anyone who ran them from `main` before the rename.
  See [Upgrading](docs/home-assistant.md#upgrading). The
  Net Cost entities are renamed **Amount Due** (`net_cost_today` /
  `net_cost_cycle` become `amount_due_today` / `amount_due_cycle`), and the
  backfill's `tariffkit:<profile>_net_cost` statistic becomes
  `tariffkit:<profile>_amount_due`.

  The rename is the point, not a side effect. The figure changed meaning and
  sign -- it was charges less every credit earned, which goes negative; it is
  now what a statement charges, which does not. Home Assistant's statistics
  compiler accumulates a `total` sensor as `sum += new - old` whenever
  `last_reset` is unchanged, and the cycle entity's `last_reset` is the cycle
  start, so keeping the old id would have added the whole banked balance to the
  lifetime sum in a single compile and never washed it out. A new unique id
  abandons the old series intact rather than corrupting it.

  Update any dashboard card, template, or automation that names the old
  entities, and re-run the backfill: history published by the older code was
  priced as `Bill.total` and without the annual settlements. The old
  `net_cost_*` entities are removed from the registry on reload; their recorded
  statistics remain and can be deleted under **Developer tools -> Statistics**.

### Fixed
- `run_true_ups` no longer emits a Community Choice Aggregator cash-out for a
  bundled account. There is no aggregator to settle with, and on such an
  account PG&E supplies generation, so the cash-out and the Relevant Period
  each clawed back the same generation credit for the same exported energy.

- `apply_credits` no longer reports a negative `cash_due`. `non_offsettable`
  can go negative on its own -- `baseline_credit` is a negative import
  component listed there -- and at a high export-to-import ratio it outweighs
  the charges beside it. Credit that cannot be spent stays in the bank instead.

- Statement evidence is identified by what a statement says rather than by the
  bytes it arrived in, so re-importing evidence a profile already holds is a
  no-op. The utility regenerates a bill PDF on every request -- the same
  statement downloaded twice is byte-different and hashes differently -- and
  `AccountObservation.identity()` preferred that digest, so nothing a profile
  held ever matched and every `account sync --apply` appended its whole window
  again. Profiles written before this collapse their repeats the next time they
  are loaded, so no migration is needed. `source_digest` remains as provenance,
  and a profile still refuses to hold two observations that name the same source
  document while disagreeing about what it says -- keyed on the top-level digest
  falling back to the agreements' own, so an observation carrying only the
  latter is still guarded. The extraction mode is excluded from identity
  alongside the digest: the parser falls back to OCR for older statements, and
  how a document was read is not part of what it says.
- `tariffkit account sync` now signs in before asking the portal for the
  statement list. A resumed session arrives with a live session cookie and no
  CSRF token, because the token is one-shot and deliberately not cached, so the
  first authenticated call failed -- either as a bare "the session token is
  stale" or, when the portal answered with an empty list instead of an error, as
  a silent "received 0 statement update(s)" against an account with 25
  statements. `apex`'s own recovery could not rescue it: it falls back to a
  forced re-login, which fails while already signed in because the login page
  redirects to the community and the token it carries belongs to the wrong
  Lightning app. `audit doctor` was unaffected because it calls `login()` first,
  which is what made the two disagree.

## [0.3.0] - 2026-08-22

### Added
- PG&E's complete active single-family residential lineup is now covered:
  generated E-1 and E-TOU-D snapshots join E-ELEC, E-TOU-C, and EV2-A.
  E-1 preserves tiered baseline billing while exposing the over-baseline
  marginal price, and E-TOU-D observes its weekday-only 5–8 p.m. peak and
  tariff holiday calendar.
- Generated, effective-dated D-CARE, D-MEDICAL, Rule 19 Medical Baseline, and
  E-RSMART data now drive residential program adjustments. SmartRate accepts
  explicit announced event dates and marks prices beyond the authoritative
  event horizon incomplete rather than guessing future events.
- Prices now decompose into a fixed set of chartable component groups —
  generation, distribution, transmission, surcharges, credits, and a catch-all
  other on the import side; generation, delivery, credits, and other on the
  export side. The groups sum back to the price they came from and do not vary
  with the tariff, supplier, or discount, so a chart built against them
  survives an account change. `ImportPrice.grouped()`, `ExportPrice.grouped()`,
  a `groups` key in every `to_dict` payload, and `tariffkit.components` expose
  them to library and REST callers.
- Home Assistant gains a sensor per component group in each direction, and the
  forecast's `rates` attribute carries the same roll-up per hour, so both the
  recorded past and the next 48 hours can be drawn as stacked charts -- one
  card each, because stacking recorded and forecast points together would
  double-count the current hour. The MQTT publisher publishes the same series
  with matching discovery payloads, each band carrying its price's quality
  flags.
- Home Assistant now exposes the AB 205 Base Services Charge as **Daily Fixed
  Charge** in `USD/day`. The unit keeps it out of the Energy dashboard's price
  pickers and out of any `USD/kWh` stack, which is why it can be published at
  all: it is a fixed daily amount, not a marginal price.

### Changed
- Home Assistant now offers every active PG&E residential schedule and
  Medical Baseline configuration while preserving existing profile and entity
  identities.

### Fixed
- Predbat attributes now use its `from` / `to` / `rate` contract for values
  already expressed in cents. Predbat no longer interprets TariffKit's cents as
  currency units and multiplies them by 100, while Pacific-midnight anchoring
  and complete 46-, 48-, and 50-slot tariff days remain unchanged.

## [0.2.3] - 2026-08-17

### Changed
- Pacific Gas and Electric now has the unambiguous machine identifier
  `pacific_gas_and_electric`, while Home Assistant and MQTT present `PG&E` or
  the full company name. The separate `pge` identifier correctly means Portland
  General Electric and is recognized but explicitly unsupported for pricing,
  preventing it from ever selecting California tariff data.
- Home Assistant now labels the export-minus-import calculation explicitly,
  translates time-of-use states for display, and presents the forecast horizon
  as **Rates Available Through**. Forecast metadata and a new **Rate Data
  Status** entity are grouped under diagnostics, where PTO date, export lock
  end, NBT vintage, tariff provenance, source, and quality flags explain the
  active rates without crowding the primary price controls.

### Fixed
- The PyPI project page now loads the TariffKit banner from an absolute URL
  instead of an unresolved repository-relative path.

## [0.2.2] - 2026-08-16

### Added
- Project documentation now uses a TariffKit banner built from the integration's
  existing icon and a provider-neutral electricity rate curve.
- Public contribution and support guidance now provides privacy-safe issue
  forms, private security routing, review ownership, a pull request checklist,
  community conduct expectations, and exact development checks. The guidance
  also makes the generated-data and repository-only audit boundaries explicit
  so public collaboration does not expose utility-account material or alter
  distribution guarantees.
- Pull requests now run GitHub's dependency review action with read-only
  permissions so vulnerable or disallowed dependency changes fail before merge.

### Changed
- **Python 3.14.2 and Home Assistant 2026.3.0 are now the supported floors.**
  The lockfile no longer carries the obsolete Home Assistant 2026.2 fallback,
  and CI audits the complete locked dependency graph with a pinned `pip-audit`
  release while retaining raw reports as failure artifacts. The Linux secrets
  extra and Home Assistant tests both inherit the same exact cryptography pin;
  an expiring policy requires each audit to report exactly its three known,
  unreachable advisories and rejects any additional finding.
- The README now explains that default HACS approval may take months and gives
  complete custom-repository installation steps for use during the review.
- Workflow actions are pinned to reviewed immutable commits, checkout credentials
  are not persisted, superseded runs are bounded by concurrency controls, and
  repository write access is isolated to the jobs that publish results.
- MQTT now rejects credentials over plaintext unless the operator explicitly
  allows insecure authentication for an isolated trusted network. Passwords
  also require a username, while anonymous plaintext publishing remains valid.

### Fixed
- Rate-sheet regeneration now scans trailing table cells with a linear parser,
  avoiding pathological regular-expression backtracking on malformed publisher
  text while preserving accepted dollar, decimal, negative, and change-marker
  forms.

## [0.2.1] - 2026-08-16

### Added
- **HACS releases now include a deterministic `tariffkit.zip` integration
  artifact.** HACS installs only the tracked component files, rooted directly
  in the integration directory, while the release pipeline validates the ZIP
  against its source, checksums it with the Python distributions, and attaches
  all artifacts before immutable publication. Dedicated HACS and hassfest
  checks also gate integration changes and prepare TariffKit for default-store
  submission.

### Fixed
- Home Assistant action descriptions now follow hassfest's current service
  schema: icons live in `icons.json`, target metadata is explicit, and the
  config-entry-only YAML schema is declared.

## [0.2.0] - 2026-08-16

### Added
- **Named, effective-dated account profiles** track tariff, supplier, baseline,
  export, and credential-set changes over a service agreement's lifetime.
  Pricing and billing resolve the settings in force at each timestamp, including
  cycles that cross an account transition. Profile writes are atomic,
  revision-checked, and locked against concurrent updates.
- **PG&E statement import and portal synchronization** can populate an account
  profile from printed facts. Proposed changes are reported as additions,
  confirmations, conflicts, or missing required values; a statement with a
  conflict cannot be partially applied. Statement support is available through
  the `statements` extra, while the account-specific audit harness remains
  repository-only.
- **Profile-scoped meter sources** store provider-neutral grid-import and
  grid-export mappings for Home Assistant and InfluxDB 3. The bill command can
  query either source directly, with explicit command options taking precedence
  over profile, environment, and global defaults.
- **Credential sets backed by the operating-system keyring** hold PG&E, Home
  Assistant, InfluxDB, and MQTT secrets outside configuration files and command
  arguments. Environment injection remains available for containers.
- **A complete billing layer** now covers interval netting, coverage warnings,
  baseline credits, fixed charges, taxes, export-credit buckets, annual true-up,
  and CCA cash-out. Credits retain their printed statement scope instead of
  being treated as one fungible balance.
- **Home Assistant and InfluxDB 3 interval sources** complement the Green Button
  reader. Home Assistant reads long-term statistics and prefers five-minute
  data; InfluxDB derives exact totals from cumulative-counter endpoints and
  spreads advances across the time in which they accrued.
- **E-TOU-C and EV2-A retail schedules**, complete PCIA and franchise-fee
  vintage tables, E-TOU-C baseline allowances, California's electrical-energy
  surcharge, Net Surplus Compensation data, and effective-dated PG&E and MCE
  rate snapshots extend pricing and billing beyond the original E-ELEC
  schedule.
- **Repository-only rate-data generators** rebuild every vendored dataset from
  its published source and read the rendered result back through runtime code
  before writing it. A weekly workflow checks export matrices, retail tariffs,
  ACC Plus, CCA cards, Net Surplus Compensation, holidays, and the state
  surcharge for upstream changes.
- **Home Assistant account-history flows** support staged initial setup and
  profile inspection, transition editing, statement import, and sanitized
  profile export. Stable profile-based config-entry identity survives tariff and
  supplier changes.
- **Home Assistant response actions** provide current or forecast rates in
  native and EMHASS shapes for caller-selected windows. Requests reject
  ambiguous DST-fold timestamps, misaligned windows, and horizons beyond seven
  days rather than guessing.
- **Home Assistant diagnostics and integration tests** cover config and options
  flows, migration, entities, actions, Energy dashboard compatibility,
  effective-dated provenance, DST handling, and opt-in Predbat output.
  Diagnostics deliberately omit account history, observations, credentials,
  and meter mappings.
- **Pure interoperability adapters** generate EMHASS, Predbat, and generic slot
  payloads from a `PriceCurve`, shared by Home Assistant and MQTT without either
  integration becoming a dependency.
- **Request-scoped REST configuration and account selection** allow callers to
  price one request against validated settings or a named profile. Unknown
  configuration keys and credential fields are rejected.
- **A bind-mounted Home Assistant development stack** runs the custom component
  and local package source in a real container without publishing a wheel.
- **A build-once release process and maintainer runbook** synchronize Python,
  lockfile, Home Assistant, changelog, and documentation versions. One validated
  wheel/sdist pair moves through optional TestPyPI staging, protected PyPI
  approval, PEP 740 attestations, and an immutable GitHub release.

### Changed
- **The project is now TariffKit.** The distribution, import package, CLI,
  configuration directory, environment prefix, MQTT namespace, repository
  links, and Home Assistant domain use the utility-neutral `tariffkit` identity.
  The initial data-provider scope remains PG&E and California.
- **Packaging remains one public distribution in one repository.** Optional
  features use extras and lazy imports, leaving the default runtime
  dependency-free. Rate-data tooling and the account-specific audit harness are
  excluded from wheel and sdist but retain strict lint, typing, and test
  coverage.
- **Python 3.14 is the minimum**, and Home Assistant 2026.3.0 is therefore the
  integration floor. Development dependencies use PEP 735 groups, package
  licensing uses an SPDX expression, and installed metadata is the runtime
  version source.
- **Home Assistant installs the exact matching TariffKit release** instead of
  vendoring package source. Its config flow asks only questions made relevant
  by earlier answers and separates account history from forecast and Predbat
  options.
- **Home Assistant entities are lean and recorder-safe.** Import and export
  price sensors remain compatible with the Energy dashboard, a timestamp entity
  reports forecast coverage, and large forecast or optimizer payloads are not
  recorded every minute. The fixed daily charge is no longer represented as a
  marginal price sensor.
- **EMHASS forecasts are requested through an action and Predbat output is
  opt-in.** Neither large payload is computed and attached to every entity when
  unused.
- **Pricing provenance and quality are preserved end to end.** Core, CLI, REST,
  MQTT, and Home Assistant outputs retain `locked`, `exact`, and `complete`
  states and describe contiguous provenance segments across effective-date
  boundaries.
- **Green Button names now describe the format rather than its container.**
  `read_green_button`, `GreenButtonLayout`, and `--source green-button` replace
  generic CSV terminology; the legacy source spelling remains accepted.
- **Strict typing now covers the Home Assistant custom component and all
  repository tools**, while Ruff enforces function annotations throughout the
  repository.

### Fixed
- Production publishing no longer inherits a false implicit failure from the
  intentionally skipped optional TestPyPI job. The explicit release dependency
  checks now permit protected PyPI and GitHub publication only after the build
  and draft jobs themselves succeed.
- Account-profile updates no longer race between revision checks and filesystem
  mutation, statement-derived histories reject gaps and overlaps, imported Home
  Assistant profiles cannot replace an entry's stable identity, and MQTT
  profile selection respects environment precedence.
- Baseline credits use each day and vintage's own rate, pre-PTO exports earn no
  compensation, string supplier values are normalized before branching, and
  historical pricing never borrows data from a future vintage.
- CCA generation rates are selected by PG&E schedule instead of silently using
  E-ELEC values for every plan. PCIA, franchise-fee, MCE generation, cost-relief,
  and premium data are resolved from their actual effective vintages.
- Billing coverage and interval stepping use absolute time across both DST
  transitions. Repeated autumn timestamps are disambiguated during ingest, rate
  points remain contiguous, and MQTT no longer sleeps through the second 01:00
  hour.
- Green Button ingestion handles account preambles, split date/time columns,
  unit-suffixed headers, explicit offsets, and repeated autumn wall times.
  Home Assistant and InfluxDB sources report implausible resets and missing
  coverage instead of inventing plausible energy.
- Export-rate lookups use each vintage's own holiday calendar and surface
  publisher drift beyond the verified exact range rather than silently claiming
  exact future values.
- Generator parsing no longer drops a PCIA row joined to the next page header,
  and all generated files are validated through independent runtime readers
  before replacing vendored data.

## [0.1.0] - 2026-07-28

Initial release.

### Added
- `RateEngine` with `price_now()`, `price_at()`, and `forecast()` for PG&E
  E-ELEC import prices and NEM 3.0 / Net Billing Tariff export credits.
- Vendored rate data for all five NBT vintages (NBT23/24/25/26/00), collapsed
  from PG&E's ~40 MB-per-vintage hourly files to 268 KiB total with verified
  lossless round-tripping.
- ACC Plus adder as a first-class, separately reported component.
- CCA / Direct Access support: bundled generation and PCIA are dropped, and
  delivery-only prices are flagged `complete=False` rather than understated.
- CLI: `now`, `forecast`, `info`, `mqtt`, `serve`.
- MQTT publisher with Home Assistant MQTT Discovery and a last-will
  availability topic.
- FastAPI service under the `web` extra.
- HACS-installable Home Assistant custom component.
- `tools/regen_data.py` for refreshing vendored data, plus a weekly CI job that
  fails when upstream rates change.

### Notes on upstream data
- PG&E's export files label the repeated 01:00 on the autumn DST transition as
  `HS2`, so that hour is priced as 2am. Handled in `timeutil.export_hour`.
- From 2036 onward PG&E's own hour labels stop tracking Pacific daylight time
  and NBT25/26/00 duplicate some holidays onto the following day. The verified
  boundary is recorded per vintage as `exact_through` and surfaced as
  `ExportPrice.exact`. Every year within a nine-year rate lock is exact.
- Holiday calendars are extracted per vintage from the source data rather than
  recomputed, because the vintage files disagree in far-future years.

[Unreleased]: https://github.com/eman/tariffkit/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/eman/tariffkit/releases/tag/v0.9.0
[0.8.1]: https://github.com/eman/tariffkit/releases/tag/v0.8.1
[0.8.0]: https://github.com/eman/tariffkit/releases/tag/v0.8.0
[0.7.0]: https://github.com/eman/tariffkit/releases/tag/v0.7.0
[0.6.1]: https://github.com/eman/tariffkit/releases/tag/v0.6.1
[0.6.0]: https://github.com/eman/tariffkit/releases/tag/v0.6.0
[0.5.0]: https://github.com/eman/tariffkit/releases/tag/v0.5.0
[0.4.1]: https://github.com/eman/tariffkit/releases/tag/v0.4.1
[0.4.0]: https://github.com/eman/tariffkit/releases/tag/v0.4.0
[0.3.0]: https://github.com/eman/tariffkit/releases/tag/v0.3.0
[0.2.3]: https://github.com/eman/tariffkit/releases/tag/v0.2.3
[0.2.2]: https://github.com/eman/tariffkit/releases/tag/v0.2.2
[0.2.1]: https://github.com/eman/tariffkit/releases/tag/v0.2.1
[0.2.0]: https://github.com/eman/tariffkit/releases/tag/v0.2.0
