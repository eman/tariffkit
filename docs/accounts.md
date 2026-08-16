# Named account profiles

A `Config` is one moment: one tariff, one supplier, one PTO date. A real service
agreement is not — it changes tariff, moves onto or off a CCA, or gets a new
baseline territory, and every bill after that change has to price with the
settings that were actually in force on its own days, not today's. A **named
account profile** is that history: an ordered set of complete `Config`
snapshots, each dated with the day it took effect, plus the statement evidence
that established each transition. Profiles are provider-neutral and stored
locally; the first way to populate one from evidence is PG&E's own statements.

This page is in four parts: a [tutorial](#tutorial-your-first-profile) to get
one working, [how-to guides](#how-to-guides) for specific tasks, a
[reference](#reference) for commands and file formats, and an
[explanation](#explanation) of the concepts and their boundaries.

## Tutorial: your first profile

This walks through creating a profile from your current settings, then
handing it your first PG&E statement so it can confirm — or correct — what
you told it.

### 1. Set your current settings once

If you have not already, write what you know today to the main config file
(see [Configuration](configuration.md) for every key):

```bash
mkdir -p ~/.config/tariffkit
cat > ~/.config/tariffkit/config.toml <<'EOF'
supplier = "bundled"
interconnection_year = 2026
pto_date = "2026-06-03"
acc_plus_segment = "residential"
base_services_charge_tier = 3
EOF
```

### 2. Create a profile from it

```console
$ tariffkit account init home --effective 2026-06-03
name: home
epochs
  2026-06-03  E-ELEC / bundled
observations: 0
```

`--effective` is the day this snapshot became true — here, the PTO date, since
that is when NEM 3.0 billing started. `home` is now a file under
`~/.config/tariffkit/accounts/`; see [Reference](#managed-profile-files) for
its exact shape and permissions.

### 3. Price with it

```console
$ tariffkit --account home now
2026-08-15 13:00 PDT - 14:00 PDT
  import    0.33358 $/kWh   (summer/off_peak)
  export    0.04579 $/kWh   (NBT26/Weekend)
  spread   -0.28779 $/kWh
```

Every command that prices anything (`now`, `forecast`, `info`, `bill`, `mqtt`,
`serve`) accepts `--account NAME` the same way. Make it the default so you can
drop the flag:

```toml
# ~/.config/tariffkit/config.toml
[account]
default_profile = "home"
```

### 4. Hand it your first statement

```bash
pip install 'tariffkit[statements]'
```

```console
$ tariffkit account import-statement home ~/Downloads/PGE_20260804.pdf
PGE_20260804.pdf:
  CONFIRM 2026-06-30 supplier
  CONFIRM 2026-06-30 tariff
preview only; pass --apply to save
```

This is a **preview** — nothing was written. The statement agreed with what
you already told `init` about, so every fact is `CONFIRM`, dated to the
statement's own billing-period start (2026-06-30), not the epoch's effective
date. Apply it so the profile records that this statement is the evidence
behind that snapshot:

```console
$ tariffkit account import-statement home ~/Downloads/PGE_20260804.pdf --apply
PGE_20260804.pdf:
  CONFIRM 2026-06-30 supplier
  CONFIRM 2026-06-30 tariff
```

```console
$ tariffkit account history home
name: home
epochs
  2026-06-03  E-ELEC / bundled
observations: 1
evidence 1: 2026-06-30..2026-07-28 E-ELEC
```

The PDF itself was never copied anywhere and is not referenced by path; only
the sanitized facts it printed (schedule, dates, a masked account suffix, and
the PDF's own SHA-256) were kept. See
[What a profile stores](#what-a-profile-stores) for exactly what that is.

You now have a profile that prices correctly today and will keep pricing
correctly the day your tariff, supplier, or baseline territory next changes —
covered next.

### 5. Attach the meter entities to the profile

Meter mappings are profile-scoped, not effective-dated: they identify where
this account's readings live, while tariff epochs identify which rates were in
force. Configure each source once, with a pair for grid import
(energy consumed from the grid, not whole-home load) and grid export:

```bash
tariffkit account source home set ha \
  --grid-import-entity sensor.grid_import \
  --grid-export-entity sensor.grid_export
tariffkit account source home set influx \
  --grid-import-entity eagle_100_total_energy_delivered \
  --grid-export-entity eagle_100_total_energy_received \
  --apply
```

The first command is a preview; add `--apply` when it is correct. Inspect a
mapping with `tariffkit account source home show ha --json`. Once saved,
`tariffkit bill --account home --source ha ...` or `--source influx` uses these
entities automatically.

## How-to guides

### Import a local statement PDF

You already have the PDF (from **My Energy Account → Documents**, or from
your own downloads folder) and just want it reconciled:

```bash
pip install 'tariffkit[statements]'
tariffkit account import-statement home ~/Downloads/PGE_20260804.pdf
```

Import as many at once as you like — order does not matter, each is
reconciled against the profile in turn:

```bash
tariffkit account import-statement home ~/Downloads/PGE_*.pdf --apply --json
```

Nothing is written without `--apply`. Re-importing the same PDF is a no-op —
identical evidence is recognised by its digest and produces no changes — so
it is safe to point this at a whole folder of statements repeatedly (e.g.
after downloading new ones) without double-counting anything.

PG&E statements from before November 2025 contain a text layer whose font maps
most glyphs to spaces. The parser detects that specific format and falls back
to OCR automatically, given the system tools:

```bash
# macOS
brew install tesseract poppler
# Debian/Ubuntu
apt install tesseract-ocr poppler-utils
```

Without them you get a clear error rather than a silent, possibly misread
parse. A scanned or print-to-PDF copy with no text layer is rejected; download
the original statement from the portal instead.

```
PGE_20251015.pdf carries no readable text and recognition tools are not
installed; `brew install tesseract poppler` provides both
```

An OCR reading that does not reproduce the statement's own arithmetic is
discarded rather than used, and its OCR provenance remains recorded. This
substantially reduces the risk of accepting a plausible-looking misread without
claiming OCR is infallible.

### Sync statements from the PG&E portal

Store your portal credentials once — the same ones used elsewhere for
authenticated PG&E portal access, such as a Green Button download or the
audit harness:

```bash
tariffkit credentials set pge.username
tariffkit credentials set pge.password
tariffkit account sync home --since 2026-01-01
```

This downloads every statement the portal lists since that date into a
private, mode-`0700` cache directory, parses each, reconciles the evidence,
and deletes the PDFs again once it is done — pass `--keep-statements` only if
you specifically want to keep them (they carry your name, address, and
account number, so keeping them is opt-in, not a side effect):

```bash
tariffkit account sync home --since 2026-01-01 --apply --json
```

Preview first (the default, without `--apply`) on a profile you care about,
the same as with local PDFs.

### Review and apply a conflict

A statement's evidence does not always agree with the profile. When it does
not, the change comes back typed `CONFLICT` or `MISSING_REQUIRED`, and neither
can be applied:

```console
$ tariffkit account import-statement home ~/Downloads/PGE_20260901.pdf
PGE_20260901.pdf:
  CONFLICT None account_suffix
  CONFIRM 2026-08-03 supplier
  CONFIRM 2026-08-03 tariff
preview only; pass --apply to save
```

Each line is `OUTCOME EFFECTIVE FIELD` — `None` for `effective` means the
change is not tied to a single dated snapshot (an `account_suffix` mismatch
applies to the whole profile, not one epoch). Use `--json` for the full
detail — every change carries `before`, `after`, and `reason`:

```console
$ tariffkit account import-statement home ~/Downloads/PGE_20260901.pdf --json
```
```json
{
  "profile": "home",
  "applied": false,
  "proposals": [
    {
      "profile_revision": "...",
      "changes": [
        {
          "outcome": "conflict",
          "effective": null,
          "field": "account_suffix",
          "before": ["****4821"],
          "after": ["****9999"],
          "reason": "statement account suffix differs from established profile evidence"
        },
        {
          "outcome": "confirm",
          "effective": "2026-08-03",
          "field": "supplier",
          "before": "bundled",
          "after": "bundled",
          "reason": ""
        },
        {
          "outcome": "confirm",
          "effective": "2026-08-03",
          "field": "tariff",
          "before": "EV2-A",
          "after": "EV2-A",
          "reason": ""
        }
      ]
    }
  ]
}
```

`--apply` refuses outright while any change is a conflict or a missing value:

```
error: account update contains conflicts or missing required values
```

What to do depends on which outcome you got:

- **`account_suffix` conflict** — the statement is for a different account
  than the one this profile represents. Create or use a separate profile for
  it rather than forcing the merge.
- **`agreement_overlap` conflict** — two statements' service-agreement spans
  overlap and print contradictory facts for the same days. One of them is
  wrong (or you have mis-dated a manual `account update`); re-check both
  against the actual PDFs.
- **`missing-required` for `cca` / `cca.rate_card_or_generation_rates`** — the
  statement shows CCA service starting, but the profile has no generation
  rate card or rates configured yet, and none can be guessed. Add them
  explicitly first:

  ```bash
  tariffkit account update home --effective 2026-08-03 \
    --supplier cca --cca-json '{"name": "MCE", "rate_card": "mce", "pcia_vintage": 2011}'
  ```

  then re-run the import; the statement's CCA facts will now `CONFIRM` or
  `ADD` against a complete snapshot instead of stalling.
- **`missing-required` for `agreement_period`** — the statement's
  service-agreement spans are not contiguous with what the profile already
  knows (a gap between them). Import whatever statement fills the gap, or
  establish that snapshot explicitly with `account update`.

None of this ever half-applies: a change set with any conflict or missing
value cannot be saved at all, so the profile is always either fully caught up
to a statement or untouched by it.

### Make an account change explicit, without a statement

Not every change needs to wait for a statement — you already know your
service will change (a scheduled tariff switch, a move to a CCA) and want the
profile to reflect it starting on a known day:

```bash
tariffkit account update home --effective 2027-06-01 \
  --tariff E-TOU-C --baseline-territory X --apply
```

Only the fields you name change; everything else in that snapshot carries
forward unchanged from whatever was in force the day before. Preview first by
leaving off `--apply` — nothing is written until you add it. `--note` records
why, for your own later reference:

```bash
tariffkit account update home --effective 2027-06-01 \
  --tariff E-TOU-C --note "switched off E-ELEC ahead of the winter rate change"
```

To replace a whole snapshot at once instead of naming individual fields, give
a TOML or JSON `Config`:

```bash
tariffkit account update home --effective 2027-06-01 --config new-settings.toml
# or
tariffkit account update home --effective 2027-06-01 --config-json - <<'EOF'
{"tariff": "EV2-A", "supplier": "bundled", "interconnection_year": 2026, "pto_date": "2026-06-03"}
EOF
```

A later statement that confirms the same facts will just `CONFIRM` them; one
that disagrees will surface as a conflict, exactly as in the previous guide —
an explicit update is not exempt from being checked against evidence later.

### Move a profile to Home Assistant

The CLI's export is the integration's import format — nothing is re-derived,
so the profile is unchanged, evidence and all:

```console
$ tariffkit account export home
```
```json
{"schema_version": 1, "name": "home", "credential_set": null, "epochs": [...], "observations": [...]}
```

In Home Assistant: **Settings → Devices & Services → PG&E Rates →
Configure → Import profile**, paste that text, submit. To go the other way —
copy an epoch you built in the Home Assistant options flow back out — use
**Configure → Export profile** and paste its output into a file for
`tariffkit account update ... --config-json`, or keep it only in Home
Assistant if that is where you manage it.

A profile exported this way never carries a `credential_set` — Home Assistant
strips it, since it never authenticates to PG&E and has nothing to associate
one with. See [Home Assistant](home-assistant.md#managing-account-history) for
the rest of the options-flow actions.

### Recover from an interrupted or concurrent update

**A process killed mid-write cannot corrupt the profile.** A save writes a
temporary file in the same directory, `fsync`s it, and only then atomically
replaces `<name>.json` — the replace is one filesystem operation, so the file
you already have is either the version before your edit or the version after
it, never a partial one. If the process died before the replace, at most a
stray `.{name}.*.tmp` file is left next to it; it is ignored by every command
here (`list`, `show`, `export`, ...) and safe to delete:

```bash
rm ~/.config/tariffkit/accounts/.home.*.tmp
```

**A genuinely concurrent update — two invocations racing on the same
profile — fails rather than silently overwriting.** Every save records the
exact revision it read, and a second writer whose revision has since moved
gets:

```
error: profile 'home' changed; reload it before saving
```

with exit code `1`, and nothing is written. Recover by re-reading the current
state and reapplying your change on top of it:

```bash
tariffkit account show home        # see what actually landed
tariffkit account update home --effective 2027-06-01 --tariff E-TOU-C --apply
```

This is the same protection for a scheduled `account sync` racing an
interactive `account update` as for two terminals — whichever writes second
is told to reload, rather than winning silently and discarding the first
writer's change.

## Reference

### Commands

All under `tariffkit account`. Every mutating command previews by default;
add `--apply` to write. `--json` on any of them emits machine-readable
output instead of the human summary shown above.

| Command | Does |
|---|---|
| `account init NAME [--effective DATE] [--config PATH \| --config-json PATH] [--credential-set SET] [--audit-file PATH] [--json]` | Create a profile. `--audit-file` explicitly migrates legacy audit history; otherwise one epoch comes from `--config`, `--config-json`, or the resolved main `Config`. Repository-local audit configuration is never read implicitly. |
| `account list [--json]` | List profile names. |
| `account show NAME [--json]` | Print a profile's epochs. |
| `account history NAME [--json]` | Print epochs and the statement evidence recorded against them. |
| `account update NAME --effective DATE [field flags...] [--config PATH \| --config-json PATH] [--note TEXT] [--credential-set SET] [--apply] [--json]` | Add or replace one dated snapshot. Field flags (`--tariff`, `--supplier`, `--interconnection-year`, `--pto-date`, `--vintage`, `--acc-plus-segment`, `--discount`, `--base-services-charge-tier`, `--baseline-territory`, `--baseline-code`, `--nsc-rate`, `--cca-json`) change only the named fields against the snapshot in force the day before; `--config`/`--config-json` replace the whole snapshot. |
| `account import-statement NAME PDF... [--apply] [--json]` | Parse local PDFs and reconcile their evidence. |
| `account sync NAME [--config PATH] [--since DATE] [--apply] [--keep-statements] [--json]` | Download portal statements since a date and reconcile them. |
| `account export NAME [--output PATH] [--json]` | Print (or write, mode `0600`) the sanitized profile JSON — the Home Assistant import format. |
| `account source NAME show {ha,influx} [--json]` | Show the profile's grid-import/grid-export entities for one meter source. |
| `account source NAME set {ha,influx} --grid-import-entity ID --grid-export-entity ID [--apply] [--json]` | Preview or save a provider-neutral meter mapping. It is not effective-dated. |

`--config` means two different things above: on `init`/`update` it is a
`Config` snapshot (TOML or, with `--config-json`, JSON) to load as the
epoch's settings; on `sync` it is the main `config.toml` to read portal
connection settings from (irrelevant if the profile has a `--credential-set`,
in which case those secrets are used instead). `tariffkit account --help` and
`tariffkit account <command> --help` are authoritative for exact flags.

### Selecting a profile

`--account NAME` on `now`, `forecast`, `info`, `bill`, `mqtt`, and `serve`
selects a profile explicitly (before or after the subcommand name, both
work). It cannot be combined with `--config` — that combination is rejected
with `--account cannot be combined with --config`, because `--config` is the
explicit stateless alternative, not a modifier on a profile.

Without either flag, resolution is:

1. `TARIFFKIT_ACCOUNT` or `TARIFFKIT_PROFILE` (checked in that order).
2. `[account] default_profile` (or `profile` / `default`) in the main
   `config.toml` — or a bare `default_profile`/`profile`/`account_profile`
   key at the top level.
3. No profile: the stateless `Config.load()` path, exactly as before profiles
   existed.

`tariffkit info` (with or without `--account`) always shows what actually
resolved, including `account_profile` and the resolved `account_effective`
snapshot when a profile is active.

### REST profile selection

`create_app()` accepts `profile_repository=` and starts with a configured
default profile the same way the CLI does. Every `POST` pricing endpoint
(`/v1/meta`, `/v1/price/now`, `/v1/price/at`, `/v1/forecast`) accepts a
`profile` (or `account` — the two must agree if both are given) key selecting
an existing local profile for that request only, alongside the existing
`config` key; supplying both is rejected with 422. There is no endpoint to
list, create, edit, or delete a profile, and none accepts a PDF or a
credential — those are CLI-only, and a name that does not resolve returns
`404 {"detail": "profile unavailable"}` regardless of whether it is absent,
malformed, or unreadable, so a probe cannot learn which. See
[REST API](web.md#named-account-profiles).

### Managed profile files

Stored at `$XDG_CONFIG_HOME/tariffkit/accounts/<name>.json` (default
`~/.config/tariffkit/accounts/`), directory mode `0700`, file mode `0600`.
Names are validated as a lowercase slug (`[a-z0-9][a-z0-9_-]*`, ≤ 64 chars)
before touching a path, and a symlink anywhere in the path is refused rather
than followed. A save validates a temporary file in the same directory,
`fsync`s it, checks the on-disk revision has not moved since it was read, and
only then atomically replaces the target — see
[Recover from an interrupted or concurrent update](#recover-from-an-interrupted-or-concurrent-update).

Top-level shape:

```json
{
  "schema_version": 1,
  "name": "home",
  "credential_set": null,
  "meter_sources": {
    "ha": {
      "grid_import_entity": "sensor.grid_import",
      "grid_export_entity": "sensor.grid_export"
    },
    "influx": null
  },
  "epochs": [
    {"effective": "2026-06-03", "config": { "...": "a complete Config.to_dict()" }, "note": ""}
  ],
  "observations": [
    {
      "agreements": [
        {
          "provider": "pge",
          "statement_date": "2026-08-01",
          "period": {"start": "2026-07-03", "end": "2026-08-01"},
          "tariff": "EV2-A",
          "supplier": "bundled",
          "cca_identity": null,
          "baseline_territory": null,
          "pcia_vintage": null,
          "account_suffix": "****4821",
          "extraction_mode": "text",
          "source_digest": "<sha-256 of the source PDF>"
        }
      ],
      "source_digest": "<sha-256 of the source PDF>",
      "observed_at": "2026-08-02"
    }
  ]
}
```

`schema_version` is checked on load; a file from a newer schema than this
install understands is rejected rather than partially trusted. `epochs[].config`
is exactly `Config.to_dict()` — the same shape as `--config-json` input and
`/v1/meta`'s `account_effective`. See
[What a profile stores](#what-a-profile-stores) for what evidence deliberately
excludes.

`meter_sources` is optional when reading older schema-1 profiles, so existing
profiles (including managed profiles created before meter mappings existed)
migrate to empty source settings. New files serialize both optional providers.
The mapping is deliberately outside `epochs`: changing a data source must not
reprice historical tariff snapshots.

### Extras

| Extra | Adds |
|---|---|
| `tariffkit[statements]` | `pypdf`, for reading local statement PDFs. |
| `tariffkit[pge]` | `httpx`, for the authenticated portal session `account sync` uses. |
| `tariffkit[secrets]` | `keyring`, for `tariffkit credentials` and named credential sets. |
| `tariffkit[all]` | Every extra, including the three above. |

Poppler (`pdftoppm`) and Tesseract are system tools, not Python packages;
they are only invoked for older PG&E statements whose embedded font maps its
glyphs to spaces. Their absence is reported with an install hint rather than
failing silently.

### Credential sets

A profile's `credential_set` is a name, not a secret — it selects which
*named keyring entry* `account sync` reads, so more than one profile can
share one PG&E login without storing the password twice:

```bash
tariffkit credentials set pge.username --set rentals
tariffkit credentials set pge.password --set rentals
tariffkit account init unit_a --credential-set rentals
tariffkit account init unit_b --credential-set rentals
```

Without `--credential-set`, `account sync` falls back to the same unnamed
credential storage other authenticated PG&E portal access (a Green Button
download, the audit harness) uses. `tariffkit credentials list --set rentals`
shows which names are populated, never their values.

## Explanation

### Account history, statement evidence, and published tariffs are three different things

It is easy to conflate these because a bill mixes them on one page, but they
answer different questions and come from different places:

- **Published tariff vintages** (`src/tariffkit/data/tariff/`,
  `src/tariffkit/data/export/`) are what PG&E filed and the CPUC adopted —
  facts about the world, true for everyone on that schedule, versioned by
  effective date and regenerated from the filing itself
  (see [Maintaining rate data](data.md)). They answer "what does E-ELEC cost
  in October 2026?"
- **Account history** (an `AccountProfile`'s `epochs`) is facts about *your*
  service agreement — which tariff, which supplier, which baseline
  territory, and from when. It answers "what was *my* configuration on
  15 March?" by pointing at the vintage that applied then; it does not
  duplicate the vintage's numbers.
- **Statement evidence** (`observations`) is what a specific PG&E document
  printed, kept only to justify why an epoch exists and to make re-importing
  it a no-op. It answers "how do we know that?" — and is explicitly not
  authoritative on its own. `reconcile()` only ever proposes a change to an
  epoch; nothing in a statement writes itself.

A profile can therefore have an epoch with no observation behind it (you
told `account update` directly) and an observation that changes nothing
(a statement that confirms a fact you already knew). Neither case is an
error, and `account history` prints both so the distinction stays visible.

### Why an epoch is inferred only from evidence the statement actually shows

`reconcile()` only ever proposes what a statement printed, dated to the exact
day it says a service agreement began — never a date it merely implies. A
statement covering 3 July–1 August under a new tariff is exact evidence that
the change took effect *on or before* July 3rd, because the utility cannot
print a cycle boundary it did not act on; it is not evidence about what
changed on July 3rd specifically versus some earlier day the account holder
already knew about but this statement does not mention. That is why
`account update` exists as a separate, explicit path: some facts (this
CCA's product tier, a PTO year, a discount code) are never printed anywhere
and have no correct guess, so they require either an established invariant
elsewhere in the profile or your own input, and `reconcile()` reports them as
`missing-required` rather than inventing a plausible value.

The same caution applies going the other direction in time. A later bill
cannot establish an earlier boundary either: printing a change *effective*
July 3rd is evidence about July 3rd, not about which day in June the
customer actually signed up. Previous bills are read for what they show, not
mined for inference past what they show.

### Why billing-cycle boundaries matter here

`BillEngine` prices one cycle against one `Config`. A profile's
`segments_for(period)` is what makes billing a cycle that spans a transition
correct instead of averaged-out wrong: it tiles the requested period into
one `Segment` per epoch active during it, each priced with its own snapshot,
and `tariffkit bill --account NAME` uses this automatically instead of a
single `Config`. See [Bill calculator](billing.md#named-account-profiles).

This is also why `reconcile()` treats a statement's own cycle boundaries as
strong evidence in the first place: PG&E's own statement is the one document
that has to state exactly where a mid-cycle rate or schedule change fell,
because it prices across that boundary too (see `audit/README.md`'s worked
example of a cycle split by a rate change). A profile's segmentation and a
statement's own service-agreement split are answering the same question from
two directions, which is what makes one a check on the other.

### Local, open-source processing — not upload

"Private" described where the PG&E statement parser used to live
(`audit/`, unpublished), never what it did to a PDF. Nothing here changes
that: `tariffkit account import-statement` and `account sync` read a PDF —
one already on your disk, or one downloaded straight from your own
authenticated portal session — parse it in this process, and keep only the
sanitized facts described in
[What a profile stores](#what-a-profile-stores). The PDF's bytes are never
sent anywhere by this library; there is no server this project runs, and
none of this code opens an outbound connection to anything other than PG&E's
own portal, only in `account sync`, only with your own credentials, only to
fetch documents you are entitled to.

Making the parser public changed its *distribution* — it now ships as
regular, reviewable open-source code under `tariffkit[statements]` instead of
living only in this repository's own maintainer harness — not its data
handling. What stays repository-only is the part that is genuinely specific
to reconciling *this project's own* real statements against computed bills:
PG&E's line-to-component mapping, attribution rules, run orchestration, and
portal-protocol research. See
[Packaging strategy](packaging_strategy.md) for that boundary.

### What a profile stores

Deliberately excluded from every managed file and from every observation,
by construction rather than by convention:

- the PDF's text or any line item, amount, or balance it printed;
- an unmasked account number (`account_suffix` keeps at most the last four
  digits, e.g. `****4821`);
- PG&E, Home Assistant, InfluxDB, or MQTT credentials, cookies, or tokens —
  those stay in the OS keyring, referenced only by the profile's
  `credential_set` *name*;
- anything not printed by the statement itself — an unobserved fact is
  reported as `missing-required`, never filled in with a plausible guess.

What is kept: the resolved `Config` for each epoch, the optional provider
meter mappings, a short human note, and,
per observation, the provider, statement date, exact period, printed
schedule/supplier/baseline/PCIA facts, a masked account suffix, whether
extraction used the text layer or OCR, and the source PDF's SHA-256 (to
recognise the same statement again without keeping it).
