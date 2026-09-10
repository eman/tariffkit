# Your account

A `Config` is one moment: one tariff, one supplier, one PTO date. A real service
agreement is not — it changes tariff, moves onto or off a CCA, or gets a new
baseline territory, and every bill after that change has to price with the
settings that were actually in force on its own days, not today's. Your
**account** is that history: an ordered set of complete `Config` snapshots, each
dated with the day it took effect, plus the statement evidence that established
each transition. It is provider-neutral and stored locally; the first way to
populate one from evidence is PG&E's own statements.

There is one account, in one file, and every command uses it without being
asked. See [Why there is one account](#why-there-is-one-account) for why it is
not a set of named profiles you choose between.

This page is in four parts: a [tutorial](#tutorial-your-first-account) to get
it working, [how-to guides](#how-to-guides) for specific tasks, a
[reference](#reference) for commands and file formats, and an
[explanation](#explanation) of the concepts and their boundaries.

## Tutorial: your first account

This walks through creating your account from your current settings, then
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

### 2. Create your account from it

```console
$ tariffkit account init --effective 2026-06-03
epochs
  2026-06-03  E-ELEC / bundled
observations: 0
```

`--effective` is the day this snapshot became true — here, the PTO date, since
that is when NEM 3.0 billing started. It is now `~/.config/tariffkit/account.json`;
see [Reference](#the-account-file) for its exact shape and permissions.

Check what it resolved to:

```console
$ tariffkit account show
in force since 2026-06-03
  utility                   pacific_gas_and_electric
  tariff                    E-ELEC
  supplier                  bundled
  interconnection_year      2026
  pto_date                  2026-06-03
  ...

1 epoch, 0 statement observations -- see 'tariffkit account history'
```

`show` answers "what is my account?" — one moment, fully resolved.
`history` answers "how did it get here?" — every epoch, with the statements
that established them.

### 3. Price with it

```console
$ tariffkit now
2026-08-15 13:00 PDT - 14:00 PDT
  import    0.33358 $/kWh   (summer/off_peak)
  export    0.04579 $/kWh   (NBT26/Weekend)
  spread   -0.28779 $/kWh
```

Every command that prices anything (`now`, `forecast`, `info`, `bill`, `mqtt`,
`serve`) uses the account from here on, with no flag to remember. `--config
FILE` is how you opt out for one command and price a hypothetical instead.

### 4. Hand it your first statement

```bash
pip install 'tariffkit[statements]'
```

```console
$ tariffkit account import-statement ~/Downloads/PGE_20260804.pdf
PGE_20260804.pdf:
  CONFIRM 2026-06-30 supplier
  CONFIRM 2026-06-30 tariff
preview only; pass --apply to save
```

This is a **preview** — nothing was written. The statement agreed with what
you already told `init` about, so every fact is `CONFIRM`, dated to the
statement's own billing-period start (2026-06-30), not the epoch's effective
date. Apply it so the account records that this statement is the evidence
behind that snapshot:

```console
$ tariffkit account import-statement ~/Downloads/PGE_20260804.pdf --apply
PGE_20260804.pdf:
  CONFIRM 2026-06-30 supplier
  CONFIRM 2026-06-30 tariff
```

```console
$ tariffkit account history
epochs
  2026-06-03  E-ELEC / bundled
observations: 1
evidence 1: 2026-06-30..2026-07-28 E-ELEC
```

The PDF itself was never copied anywhere and is not referenced by path; only
the sanitized facts it printed (schedule, dates, a masked account suffix, and
the PDF's own SHA-256) were kept. See
[What the account stores](#what-the-account-stores) for exactly what that is.

You now have an account that prices correctly today and will keep pricing
correctly the day your tariff, supplier, or baseline territory next changes —
covered next.

### 5. Attach the meter entities

Meter mappings belong to the account as a whole, not to a date: they identify
where its readings live, while tariff epochs identify which rates were in
force. Configure each source once, with a pair for grid import
(energy consumed from the grid, not whole-home load) and grid export:

```bash
tariffkit account source set ha \
  --grid-import-entity sensor.grid_import \
  --grid-export-entity sensor.grid_export
tariffkit account source set influx \
  --grid-import-entity eagle_100_total_energy_delivered \
  --grid-export-entity eagle_100_total_energy_received \
  --apply
```

The first command is a preview; add `--apply` when it is correct. Inspect a
mapping with `tariffkit account source show ha --json`. Once saved,
`tariffkit bill --source ha ...` or `--source influx` uses these entities
automatically.

## How-to guides

### Import a local statement PDF

You already have the PDF (from **My Energy Account → Documents**, or from
your own downloads folder) and just want it reconciled:

```bash
pip install 'tariffkit[statements]'
tariffkit account import-statement ~/Downloads/PGE_20260804.pdf
```

Import as many at once as you like — order does not matter, each is
reconciled against the account in turn:

```bash
tariffkit account import-statement ~/Downloads/PGE_*.pdf --apply --json
```

Nothing is written without `--apply`. Re-importing the same statement is a
no-op — evidence is recognised by *what it says*, not by the bytes it arrived
in — so it is safe to point this at a whole folder of statements repeatedly
(e.g. after downloading new ones) without double-counting anything.

That distinction matters for `account sync`, which fetches from the portal
rather than reading a saved file. PG&E regenerates a bill PDF on every request,
so the same statement downloaded twice is byte-different and hashes
differently; identifying evidence by its digest would make every sync look like
new evidence and append the whole window again. Identity is a hash of the
agreements' own facts instead, with the digest and the extraction mode left out
as provenance. `statement_date` is part of those facts, so a genuinely
re-issued or corrected statement still counts as its own evidence — only true
repeats collapse.

An account that already accumulated duplicates repairs itself: identity is
computed on load, so the repeats collapse the next time it is read and the file
is rewritten without them.

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
tariffkit account sync --since 2026-01-01
```

This downloads every statement the portal lists since that date into a
private, mode-`0700` cache directory, parses each, reconciles the evidence,
and deletes the PDFs again once it is done — pass `--keep-statements` only if
you specifically want to keep them (they carry your name, address, and
account number, so keeping them is opt-in, not a side effect):

```bash
tariffkit account sync --since 2026-01-01 --apply --json
```

Preview first (the default, without `--apply`), the same as with local PDFs.

### Review and apply a conflict

A statement's evidence does not always agree with the account. When it does
not, the change comes back typed `CONFLICT` or `MISSING_REQUIRED`, and neither
can be applied:

```console
$ tariffkit account import-statement ~/Downloads/PGE_20260901.pdf
PGE_20260901.pdf:
  CONFLICT None account_suffix
  CONFIRM 2026-08-03 supplier
  CONFIRM 2026-08-03 tariff
preview only; pass --apply to save
```

Each line is `OUTCOME EFFECTIVE FIELD` — `None` for `effective` means the
change is not tied to a single dated snapshot (an `account_suffix` mismatch
applies to the whole account, not one epoch). Use `--json` for the full
detail — every change carries `before`, `after`, and `reason`:

```console
$ tariffkit account import-statement ~/Downloads/PGE_20260901.pdf --json
```
```json
{
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
          "reason": "statement account suffix differs from established account evidence"
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
  ],
  "skipped": []
}
```

`--apply` refuses outright while any change is a conflict or a missing value:

```
error: account update contains conflicts or missing required values
```

What to do depends on which outcome you got:

- **`account_suffix` conflict** — the statement is for a different service
  agreement from the one this account represents. Check that you downloaded
  the right PDF rather than forcing the merge; if you genuinely bill two
  agreements, keep them in separate config homes (`XDG_CONFIG_HOME`), because
  one file is one agreement.
- **`agreement_overlap` conflict** — two statements' service-agreement spans
  overlap and print contradictory facts for the same days. One of them is
  wrong (or you have mis-dated a manual `account update`); re-check both
  against the actual PDFs.
- **`missing-required` for `cca` / `cca.rate_card_or_generation_rates`** — the
  statement shows CCA service starting, but the account has no generation
  rate card or rates configured yet, and none can be guessed. Add them
  explicitly first:

  ```bash
  tariffkit account update --effective 2026-08-03 \
    --supplier cca --cca-json '{"name": "MCE", "rate_card": "mce", "pcia_vintage": 2011}'
  ```

  then re-run the import; the statement's CCA facts will now `CONFIRM` or
  `ADD` against a complete snapshot instead of stalling.
- **`missing-required` for `agreement_period`** — the statement's
  service-agreement spans are not contiguous with what the account already
  knows (a gap between them). Import whatever statement fills the gap, or
  establish that snapshot explicitly with `account update`.

None of this ever half-applies: a change set with any conflict or missing
value cannot be saved at all, so the account is always either fully caught up
to a statement or untouched by it.

### Make an account change explicit, without a statement

Not every change needs to wait for a statement — you already know your
service will change (a scheduled tariff switch, a move to a CCA) and want the
account to reflect it starting on a known day:

```bash
tariffkit account update --effective 2027-06-01 \
  --tariff E-TOU-C --baseline-territory X --apply
```

Only the fields you name change; everything else in that snapshot carries
forward unchanged from whatever was in force the day before. Preview first by
leaving off `--apply` — nothing is written until you add it. `--note` records
why, for your own later reference:

```bash
tariffkit account update --effective 2027-06-01 \
  --tariff E-TOU-C --note "switched off E-ELEC ahead of the winter rate change"
```

To replace a whole snapshot at once instead of naming individual fields, give
a TOML or JSON `Config`:

```bash
tariffkit account update --effective 2027-06-01 --config new-settings.toml
# or
tariffkit account update --effective 2027-06-01 --config-json - <<'EOF'
{"tariff": "EV2-A", "supplier": "bundled", "interconnection_year": 2026, "pto_date": "2026-06-03"}
EOF
```

A later statement that confirms the same facts will just `CONFIRM` them; one
that disagrees will surface as a conflict, exactly as in the previous guide —
an explicit update is not exempt from being checked against evidence later.

### Move your account to Home Assistant

The CLI's export is the integration's import format — nothing is re-derived,
so the history is unchanged, evidence and all:

```console
$ tariffkit account export
```
```json
{"schema_version": 1, "name": null, "epochs": [...], "observations": [...]}
```

In Home Assistant: **Settings → Devices & Services → PG&E Rates →
Configure → Import profile**, paste that text, submit. To go the other way —
copy an epoch you built in the Home Assistant options flow back out — use
**Configure → Export profile** and paste its output into a file for
`tariffkit account update --config-json`, or keep it only in Home
Assistant if that is where you manage it.

The integration keeps its account in its own config entry, and names it,
because Home Assistant identifies a config entry by something stable. The CLI
has one account and no name to give it, so `name` exports as `null` and the
integration asks you for one on import. See
[Home Assistant](home-assistant.md#account-history) for the rest of
the options-flow actions.

### Recover from an interrupted or concurrent update

**A process killed mid-write cannot corrupt the account.** A save writes a
temporary file in the same directory, `fsync`s it, and only then atomically
replaces `account.json` — the replace is one filesystem operation, so the file
you already have is either the version before your edit or the version after
it, never a partial one. If the process died before the replace, at most a
stray `.account.*.tmp` file is left next to it; it is ignored by every command
here (`show`, `history`, `export`, ...) and safe to delete:

```bash
rm ~/.config/tariffkit/.account.*.tmp
```

**A genuinely concurrent update — two invocations racing — fails rather than
silently overwriting.** Every save records the exact revision it read, and a
second writer whose revision has since moved gets:

```
error: the account changed on disk; reload it before saving
```

with exit code `1`, and nothing is written. Recover by re-reading the current
state and reapplying your change on top of it:

```bash
tariffkit account history     # see what actually landed
tariffkit account update --effective 2027-06-01 --tariff E-TOU-C --apply
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
| `account init [--effective DATE] [--config PATH \| --config-json PATH] [--audit-file PATH] [--json]` | Set up your account. `--audit-file` explicitly migrates legacy audit history; otherwise one epoch comes from `--config`, `--config-json`, or the resolved main `Config`. Repository-local audit configuration is never read implicitly. |
| `account show [--json]` | Print the settings in force today, and where they came from. |
| `account history [--json]` | Print every epoch and the statement evidence recorded against them. |
| `account update --effective DATE [field flags...] [--config PATH \| --config-json PATH] [--note TEXT] [--apply] [--json]` | Add or replace one dated snapshot. Field flags (`--tariff`, `--supplier`, `--interconnection-year`, `--pto-date`, `--vintage`, `--acc-plus-segment`, `--discount`, `--base-services-charge-tier`, `--baseline-territory`, `--baseline-code`, `--nsc-rate`, `--cca-json`) change only the named fields against the snapshot in force the day before; `--config`/`--config-json` replace the whole snapshot. |
| `account import-statement PDF... [--apply] [--json]` | Parse local PDFs and reconcile their evidence. |
| `account sync [--config PATH] [--since DATE] [--apply] [--keep-statements] [--json]` | Download portal statements since a date and reconcile them. |
| `account periods [--apply] [--json]` | Read the cycle boundaries PG&E billed on from the portal and record them on the account. |
| `account export [--output PATH] [--json]` | Print (or write, mode `0600`) the sanitized account JSON — the Home Assistant import format. |
| `account source show {ha,influx} [--json]` | Show the grid-import/grid-export entities for one meter source. |
| `account source set {ha,influx} --grid-import-entity ID --grid-export-entity ID [--apply] [--json]` | Preview or save a provider-neutral meter mapping. It is not effective-dated. |

`--config` means two different things above: on `init`/`update` it is a
`Config` snapshot (TOML or, with `--config-json`, JSON) to load as the
epoch's settings; on `sync` it is the main `config.toml` to read portal
connection settings from. `tariffkit account --help` and
`tariffkit account <command> --help` are authoritative for exact flags.

### Pricing from the account

`now`, `forecast`, `info`, `bill`, `mqtt`, and `serve` price from the account
whenever one exists. There is no flag for it and nothing to select.

`--config FILE` is the way out: it prices that file's single snapshot instead,
for a hypothetical or for someone who has not set an account up. The two are
never combined, because a `Config` is one moment and the account is a history
— a modifier on one is not meaningful to the other.

`tariffkit info` always shows what actually resolved, including
`account_profile` and the resolved `account_effective` snapshot when the
account is in use.

### The REST API and the account

`create_app(profile=...)` is handed an account to serve; it never looks for
one. `tariffkit serve` passes the account it found, and an embedder passes
whatever it holds. Every `POST` pricing endpoint (`/v1/meta`,
`/v1/price/now`, `/v1/price/at`, `/v1/forecast`) accepts a `profile` (or
`account`) key asking for that account for one request, alongside the existing
`config` key; supplying both is rejected with 422. There is no endpoint to
create, edit, or delete anything, and none accepts a PDF or a credential —
those are CLI-only. A request that asks for an account the server was not
given returns `404 {"detail": "profile unavailable"}`, which says nothing
about whether one exists on that machine. See [REST API](web.md#the-account).

### The account file

Stored at `$XDG_CONFIG_HOME/tariffkit/account.json` (default
`~/.config/tariffkit/account.json`), directory mode `0700`, file mode `0600`.
A symlink anywhere in the path is refused rather than followed. A save
validates a temporary file in the same directory, `fsync`s it, checks the
on-disk revision has not moved since it was read, and only then atomically
replaces the target — see
[Recover from an interrupted or concurrent update](#recover-from-an-interrupted-or-concurrent-update).

Reading and writing it is the command line's own job
(`tariffkit.cli.AccountStore`), not the library's: nothing under
`tariffkit.billing`, `tariffkit.web` or `tariffkit.mqtt` opens it, so an
embedder that already holds an account — Home Assistant, in its config entry
— never goes near this path.

Top-level shape:

```json
{
  "schema_version": 2,
  "name": null,
  "billing_periods": [
    {"start": "2026-07-29", "end": "2026-08-27"}
  ],
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
[What the account stores](#what-the-account-stores) for what evidence
deliberately excludes.

`name` is `null` from the CLI, which has one account and no name to give it;
Home Assistant sets it, because a config entry needs something stable to be
identified by.

`billing_periods` is what `account periods` records: the cycles the utility
says it billed, inclusive at both ends, sorted and non-overlapping. Boundaries
without the statements that print them, so anything holding the account can
price the cycle it is in without a PDF or portal credentials — which is exactly
Home Assistant's situation. `schema_version` 1 files predate the field and are
read unchanged; saving one writes 2.

`meter_sources` is optional when reading older schema-1 files, so an account
written before meter mappings existed migrates to empty source settings. New
files serialize both optional providers. The mapping is deliberately outside
`epochs`: changing a data source must not reprice historical tariff snapshots.

A lone `accounts/<name>.json` left by an older install is adopted into
`account.json` the first time a command reads it. Several are left alone:
choosing between them is a decision, and guessing it would price bills from an
agreement you did not choose.

### Extras

| Extra | Adds |
|---|---|
| `tariffkit[statements]` | `pypdf`, for reading local statement PDFs. |
| `tariffkit[pge]` | `httpx`, for the authenticated portal session `account sync` uses. |
| `tariffkit[secrets]` | `keyring`, for `tariffkit credentials`. |
| `tariffkit[all]` | Every extra, including the three above. |

Poppler (`pdftoppm`) and Tesseract are system tools, not Python packages;
they are only invoked for older PG&E statements whose embedded font maps its
glyphs to spaces. Their absence is reported with an install hint rather than
failing silently.

### Credentials

`account sync` reads the same credentials every other authenticated PG&E
access uses — a Green Button download, the audit harness — stored with
`tariffkit credentials set pge.username` and `pge.password`, or supplied as
`PGE_USERNAME`/`PGE_PASSWORD` in the environment or `~/.config/tariffkit/.env`,
which win over the keyring. One account reads one set of credentials, so there
is nothing to select. `tariffkit credentials list` shows where each name
resolves from, never its value. See
[Configuration](configuration.md#credentials).

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

The account can therefore have an epoch with no observation behind it (you
told `account update` directly) and an observation that changes nothing
(a statement that confirms a fact you already knew). Neither case is an
error, and `account history` prints both so the distinction stays visible.

### Why there is one account

There used to be named profiles and a way to select between them. An account is
one **service agreement**, not one person, and PG&E bills a service agreement
per premise — so someone with rental units does have several under one login,
and that is who the names were for.

Everyone else paid for them: a name to invent, a flag to remember on every
command, and a wrong answer when the flag was forgotten. Forgetting it did not
fail; it priced the bill from `config.toml` instead, which reads a CCA account
as bundled — one export credit bank where it has two — and prices a cycle that
crossed a rate change at a single tariff. A plausible wrong number is worse
than an error.

So there is one account, in one file, used without being asked. If you really
do bill two agreements, give each its own config home:

```bash
XDG_CONFIG_HOME=~/.config/unit-a tariffkit bill --source influx ...
```

What is kept from the old design is the part that earned its keep: the dated
history, because a bill prices with the settings in force over its own days.

### Why the history cannot live in `config.toml`

It is the same reason a bill is not priced at today's rates. `config.toml`
describes one moment, and a bill from eighteen months ago has to price on the
tariff that was in force over its own days. On a real account the June 2026
cycle crossed EV2-A to E-ELEC on the Permission To Operate date, and the two
answers are $25.47 and $24.21 — neither wrong for its own arrangement, and only
one of them that cycle's bill.

So `config.toml` keeps what is true now and has no history to carry: the
forecast horizon, which integrations are on, where the broker is. A complete
pricing config there is still read, for someone who has not set an account up
yet, and `--config` forces it — but where an account exists it is the better
answer and every command prefers it.

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
elsewhere in the account or your own input, and `reconcile()` reports them as
`missing-required` rather than inventing a plausible value.

The same caution applies going the other direction in time. A later bill
cannot establish an earlier boundary either: printing a change *effective*
July 3rd is evidence about July 3rd, not about which day in June the
customer actually signed up. Previous bills are read for what they show, not
mined for inference past what they show.

### Why billing-cycle boundaries matter here

`BillEngine` prices one cycle against one `Config`. An account's
`segments_for(period)` is what makes billing a cycle that spans a transition
correct instead of averaged-out wrong: it tiles the requested period into
one `Segment` per epoch active during it, each priced with its own snapshot,
and `tariffkit bill` uses this automatically instead of a single `Config`. See
[Bill calculator](billing.md#pricing-from-your-account).

This is also why `reconcile()` treats a statement's own cycle boundaries as
strong evidence in the first place: PG&E's own statement is the one document
that has to state exactly where a mid-cycle rate or schedule change fell,
because it prices across that boundary too (see `audit/README.md`'s worked
example of a cycle split by a rate change). The account's segmentation and a
statement's own service-agreement split are answering the same question from
two directions, which is what makes one a check on the other.

### Local, open-source processing — not upload

"Private" described where the PG&E statement parser used to live
(`audit/`, unpublished), never what it did to a PDF. Nothing here changes
that: `tariffkit account import-statement` and `account sync` read a PDF —
one already on your disk, or one downloaded straight from your own
authenticated portal session — parse it in this process, and keep only the
sanitized facts described in
[What the account stores](#what-the-account-stores). The PDF's bytes are never
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

### What the account stores

Deliberately excluded from the account file and from every observation,
by construction rather than by convention:

- the PDF's text or any line item, amount, or balance it printed;
- an unmasked account number (`account_suffix` keeps at most the last four
  digits, e.g. `****4821`);
- PG&E, Home Assistant, InfluxDB, or MQTT credentials, cookies, or tokens —
  those stay in the OS keyring, and the account file does not reference them at
  all;
- anything not printed by the statement itself — an unobserved fact is
  reported as `missing-required`, never filled in with a plausible guess.

What is kept: the resolved `Config` for each epoch, the optional provider
meter mappings, a short human note, and,
per observation, the provider, statement date, exact period, printed
schedule/supplier/baseline/PCIA facts, a masked account suffix, whether
extraction used the text layer or OCR, and the source PDF's SHA-256 (to
recognise the same statement again without keeping it).
