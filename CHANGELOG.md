# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
- A meter that restarts its counter is refused rather than silently zeroing the
  rest of the window. Readings below the running maximum are dropped as device
  artefacts, which is right for the Eagle-100's momentary zeroes and wrong for
  a counter that begins again from a lower base after a meter swap, a firmware
  reset or a wrap: every later sample sits below the old maximum, so all of
  them were discarded, and only an empty result was checked for. The bill came
  out short and entirely plausible. A run of climbing below-maximum samples now
  raises, naming the meter and the moment; single dropouts are filtered as
  before.
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
- An interconnection year past the vendored NBT tables is refused instead of
  floating. It fell through to NBT00, which left the account floating for its
  energy value while still resolving an ACC Plus row for that year -- locked
  for the adder, unlocked for everything else, `lock_end` unset, no warning.
  A year *before* the first vintage still floats, which is what floating means.
- An interconnection after the ACC Plus table ends earns no adder rather than
  raising. Schedule NBT makes the adder available to customers interconnecting
  "during the first five years of the tariff", decreasing "until the adder
  reaches zero"; the adopted table runs 2023 to 2027, so 2028 onward is zero.
- Setting both `cca.rate_card` and `cca.export_generation_rate` is refused. The
  explicit rate won, and taking that branch skipped the card's solar bonus and
  its ACC Plus adder entirely -- a 22% under-credit into the CCA's bank, with
  the export price still reporting itself complete.
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

[Unreleased]: https://github.com/eman/tariffkit/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/eman/tariffkit/releases/tag/v0.5.0
[0.4.1]: https://github.com/eman/tariffkit/releases/tag/v0.4.1
[0.4.0]: https://github.com/eman/tariffkit/releases/tag/v0.4.0
[0.3.0]: https://github.com/eman/tariffkit/releases/tag/v0.3.0
[0.2.3]: https://github.com/eman/tariffkit/releases/tag/v0.2.3
[0.2.2]: https://github.com/eman/tariffkit/releases/tag/v0.2.2
[0.2.1]: https://github.com/eman/tariffkit/releases/tag/v0.2.1
[0.2.0]: https://github.com/eman/tariffkit/releases/tag/v0.2.0
