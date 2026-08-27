"""What a printed line is made of.

The statement and this library describe the same money with different words, and
not one-to-one: the utility combines several tariff components into a single
printed line. That combining is real and documented in the tariff, but it means a
naive label-to-key dictionary cannot express it, and a reconciler that quietly
sums "whatever is left" to make a line agree is one that will agree with anything.

So a rule names the components whose **sum** is one printed line, and a rule with
more than one component must say *why* they are combined. Two invariants keep it
honest, both enforced by :func:`check_map` and a test rather than by convention:

1. **No component key appears in more than one rule.** Otherwise one computed
   dollar is compared against two printed lines and the total over-agrees while
   every line looks right.
2. **Every multi-component rule carries a non-empty ``combines``.** An
   unexplained combination is indistinguishable from a reconciler that was tuned
   until it stopped complaining.

``verified`` records the statements on which a rule actually reconciled. An empty
tuple means nobody has confirmed it, and the report says so -- the same honesty
as ``ledger.SCOPING_VERIFIED = False``. A rule that has reconciled three times
and then fails is a genuine finding; a rule that has never reconciled is a guess.

The map lives here rather than in the library because it describes **how PG&E
prints**, on a bill-redesign cadence, and is worthless to anyone who is not this
account. Everything the library maps -- ``ledger.CREDIT_BUCKETS``,
``models.TAX_COMPONENTS`` -- is load-bearing for producing a number. This one is
load-bearing only for comparing against paper.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from tariffkit.providers.pge.statements import Section


class Side(StrEnum):
    """Which of a :class:`~tariffkit.billing.Bill`'s component maps to look in."""

    IMPORT = "import"
    EXPORT = "export"
    FIXED = "fixed"
    #: Credits *applied* this cycle, from the ledger, rather than credits
    #: earned. The statement prints both and they are not the same number: a
    #: cycle can earn $9.63 of CCA export credit and apply $3.63 of it, the rest
    #: banking. Comparing an "Applied" line against what was earned reports a
    #: mismatch on a bill that is correct.
    APPLIED = "applied"


def split_side(component: str, default: Side) -> tuple[Side, str]:
    """Resolve a possibly side-qualified component key.

    Most components live on the side their rule declares. A few do not -- the
    Base Services Charge is a fixed charge that the utility folds into otherwise
    per-kWh lines -- and a key can be written ``"fixed:base_services_charge"`` to
    say so. Qualifying explicitly rather than searching every side matters
    because some keys genuinely exist on two: ``cca_generation`` is both an
    import charge and an export credit, and guessing between them would silently
    compare a charge against a credit.
    """
    if ":" in component:
        side, _, key = component.partition(":")
        return Side(side), key
    return default, component


@dataclass(frozen=True, slots=True)
class LineRule:
    #: The printed line this rule reports under. When ``aliases`` names further
    #: printed lines, they are grouped into this one comparison -- necessary
    #: when the utility splits one computed component across several printed
    #: lines at a ratio it does not publish.
    label: str
    section: Section
    side: Side
    #: Component keys whose sum equals this line (or group of lines). A key may
    #: be side-qualified, e.g. ``"fixed:base_services_charge"``.
    components: tuple[str, ...]
    #: Required when there is more than one component: why the bill combines
    #: them. Enforced, because an unexplained combination cannot be told apart
    #: from a fudge.
    combines: str = ""
    #: Other spellings of the same line seen on other statements.
    aliases: tuple[str, ...] = ()
    #: Statement dates on which this rule actually reconciled.
    verified: tuple[date, ...] = ()

    @property
    def confirmed(self) -> bool:
        return bool(self.verified)


def normalize_label(raw: str) -> str:
    """Fold a printed label to something comparable.

    "Competition Transition Charges (CTC)" and "Competition Transition Charges"
    are the same line. Modelled on ``greenbutton._normalize``, which folds column
    headings for the same reason.
    """
    text = re.sub(r"\(.*?\)", " ", raw.lower())
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


#: The statements each rule has been confirmed against, by statement date. A
#: rule carrying dates has reproduced a printed figure; one carrying none is a
#: hypothesis, and the report says which is which rather than presenting both as
#: equally trustworthy.
DEC = date(2026, 1, 7)
JAN = date(2026, 2, 5)
APR = date(2026, 5, 6)
#: The first statement priced under the Solar Billing Plan, and so the only one
#: that exercises any of the export-credit rules at all.
AUG = date(2026, 8, 4)
#: Lines every statement prints, on both schedules.
ALL_THREE = (DEC, JAN, APR)
#: Baseline schedules only; EV2-A has no baseline allowance to credit.
BASELINE_ONLY = (DEC, JAN)

#: The map. Single-component rules are direct correspondences. Multi-component
#: rules are hypotheses until a statement confirms them, and say so.
MAP: tuple[LineRule, ...] = (
    # --- the utility's unbundled breakdown -------------------------------
    LineRule(
        "Nuclear Decommissioning",
        Section.PGE_BREAKDOWN,
        Side.IMPORT,
        ("nuclear_decommissioning",),
        verified=ALL_THREE,
    ),
    LineRule(
        "Wildfire Fund Charge",
        Section.PGE_BREAKDOWN,
        Side.IMPORT,
        ("wildfire_fund_charge",),
        verified=ALL_THREE,
    ),
    LineRule(
        "Wildfire Hardening Charge",
        Section.PGE_BREAKDOWN,
        Side.IMPORT,
        ("wildfire_hardening",),
        verified=ALL_THREE,
    ),
    LineRule(
        "Recovery Bond Charge",
        Section.PGE_BREAKDOWN,
        Side.IMPORT,
        ("recovery_bond_charge",),
        verified=ALL_THREE,
    ),
    LineRule(
        "Recovery Bond Credit",
        Section.PGE_BREAKDOWN,
        Side.IMPORT,
        ("recovery_bond_credit",),
        verified=ALL_THREE,
    ),
    LineRule(
        "Competition Transition Charges",
        Section.PGE_BREAKDOWN,
        Side.IMPORT,
        ("competition_transition_charges",),
        aliases=("Competition Transition Charges (CTC)",),
        verified=ALL_THREE,
    ),
    LineRule(
        "Energy Cost Recovery Amount",
        Section.PGE_BREAKDOWN,
        Side.IMPORT,
        ("energy_cost_recovery",),
        verified=ALL_THREE,
    ),
    LineRule(
        "PCIA",
        Section.PGE_BREAKDOWN,
        Side.IMPORT,
        ("pcia",),
        aliases=("bundled_pcia",),
        verified=ALL_THREE,
    ),
    # Confirmed from the statement's own two presentations rather than assumed:
    # the delivery detail prints Franchise Fee Surcharge per sub-period as
    # 0.14 + 0.52, and the breakdown prints "Taxes and Other" as 0.66.
    LineRule(
        "Taxes and Other",
        Section.PGE_BREAKDOWN,
        Side.IMPORT,
        ("franchise_fee_surcharge",),
        verified=ALL_THREE,
    ),
    # --- combined lines, confirmed against a statement -------------------
    LineRule(
        "Transmission",
        Section.PGE_BREAKDOWN,
        Side.IMPORT,
        ("transmission", "transmission_rate_adjustments", "reliability_services"),
        combines="all three are transmission-level charges and the bill prints "
        "their sum. Reliability services belongs here rather than with "
        "distribution, which is what the 2026-02-05 statement settled: with it "
        "under Distribution, Transmission was short by exactly its 0.15 and "
        "Distribution was over by the same amount",
        verified=ALL_THREE,
    ),
    LineRule(
        "Distribution + Public Purpose Programs",
        Section.PGE_BREAKDOWN,
        Side.IMPORT,
        (
            "distribution",
            "new_system_generation",
            "public_purpose_programs",
            "fixed:base_services_charge",
            "applied:delivery",
            "applied:bonus",
        ),
        aliases=("Distribution", "Electric Public Purpose Programs"),
        combines="two printed lines taken together, because from 2026-03-01 the "
        "utility spreads the Base Services Charge across both rather than "
        "printing it separately -- on 2026-05-06 Distribution ran 13.98 above "
        "the computed distribution and Public Purpose Programs ran 9.02 above "
        "its own, summing to the 23.01 daily charge. The split ratio is not "
        "published, so inventing one to separate the lines would be fitting the "
        "map to the answer; grouping them checks the same money without "
        "pretending to know how it was apportioned. New system generation is "
        "here too: it recovers distribution-level costs and has no line. "
        "Once solar is interconnected the breakdown is printed POST-credit: "
        "the Solar Billing Plan nets applied export and bonus credits into "
        "these same two lines rather than showing them separately, which is "
        "the whole of the 7.97 this line was out by on 2026-08-04 "
        "(6.25 + 1.71). Subtracting them here is safe on every earlier "
        "statement because nothing is exported for credit before the "
        "Permission To Operate date, so both terms are zero.",
        verified=(*ALL_THREE, AUG),
    ),
    LineRule(
        "Conservation Incentive",
        Section.PGE_BREAKDOWN,
        Side.IMPORT,
        ("conservation_incentive_adjustment", "baseline_credit"),
        combines="the baseline credit IS the conservation incentive on a "
        "baseline schedule: the delivery detail prints the credit on its own row "
        "per sub-period, and the unbundled breakdown folds it into the "
        "adjustment. 63.09 + -28.87 = 34.22 against a printed 34.25",
        verified=BASELINE_ONLY,
    ),
    # --- the generation provider's page ----------------------------------
    LineRule(
        "Generation",
        Section.CCA_GENERATION,
        Side.IMPORT,
        ("cca_generation",),
        aliases=(
            "Off Peak Winter",
            "Peak Winter",
            "Part Peak Winter",
            "Off Peak Summer",
            "Peak Summer",
            "Part Peak Summer",
        ),
        combines="the provider prints one row per season and time-of-use period; "
        "the library computes a single generation total across them",
        verified=ALL_THREE,
    ),
    LineRule(
        "Energy Commission Tax",
        Section.CCA_GENERATION,
        Side.IMPORT,
        ("energy_commission_tax",),
        verified=ALL_THREE,
    ),
    LineRule(
        "Cost Relief Credit",
        Section.CCA_GENERATION,
        Side.IMPORT,
        ("cca_cost_relief_credit",),
        aliases=("MCE Cost Relief Credit",),
        verified=(APR,),
    ),
    # Solar Billing Plan export compensation, which only exists once the system
    # is interconnected -- 2026-06-03 for this account.
    LineRule(
        "Solar Bonus Credit",
        Section.CCA_GENERATION,
        Side.EXPORT,
        ("cca_solar_bonus",),
        aliases=("MCE Solar Bonus Credit",),
        verified=(AUG,),
    ),
    # Applied, not earned. The CCA earns export credit on everything it is sent
    # and spends only what this cycle's generation charges can absorb -- 9.63
    # earned against 3.63 applied on 2026-08-04, the rest banking. Comparing
    # this line against what was earned reports a mismatch on a correct bill.
    #
    # The bonus line is grouped in rather than given a rule of its own: the CCA
    # prints it and always at 0.00, because its bonus credit banks instead of
    # being applied -- the EEBC balance on the same page grows by the full
    # adder every cycle. Left unclaimed it reads as a line nobody understands.
    #
    # That $0.00 is why nothing here caught the EEBC going unmodelled for a
    # release: this map reconciles charges, and a credit that is never applied
    # never reaches one. The balances it banks into are printed as free text
    # beside the section rather than as lines, so they are checked in
    # tests/test_ledger.py against the figures, not here.
    LineRule(
        "Energy Export Credits Applied",
        Section.CCA_GENERATION,
        Side.APPLIED,
        ("generation",),
        aliases=("Energy Export Bonus Credits Applied",),
        verified=(AUG,),
    ),
)


def check_map(rules: tuple[LineRule, ...] = MAP) -> list[str]:
    """Violations of the map's own invariants."""
    problems: list[str] = []

    seen: dict[tuple[Side, str], str] = {}
    claimed_labels: dict[str, str] = {}
    for rule in rules:
        for printed in (rule.label, *rule.aliases):
            folded = normalize_label(printed)
            if folded in claimed_labels and claimed_labels[folded] != rule.label:
                problems.append(
                    f"printed line {printed!r} is claimed by both "
                    f"{claimed_labels[folded]!r} and {rule.label!r}"
                )
            claimed_labels[folded] = rule.label
        for qualified in rule.components:
            component = split_side(qualified, rule.side)
            if component in seen:
                problems.append(
                    f"component {component[1]!r} on the {component[0]} side is claimed by "
                    f"both {seen[component]!r} and "
                    f"{rule.label!r}; one computed amount would be compared against two "
                    f"printed lines and the total would over-agree"
                )
            seen[component] = rule.label
        if len(rule.components) > 1 and not rule.combines.strip():
            problems.append(
                f"{rule.label!r} sums {len(rule.components)} components but does not say why; "
                f"an unexplained combination cannot be told apart from a fudge"
            )
        if not rule.components:
            problems.append(f"{rule.label!r} claims no components")
    return problems


def rule_for(section: Section, label: str) -> LineRule | None:
    """The rule claiming a printed line, by label or by any of its aliases."""
    wanted = normalize_label(label)
    for rule in MAP:
        if rule.section is not section:
            continue
        if wanted == normalize_label(rule.label):
            return rule
        if any(wanted == normalize_label(alias) for alias in rule.aliases):
            return rule

    # Second pass, and only for a line the generation provider prefixes with
    # its own name: "MCE Solar Bonus Credit" against the rule's "Solar Bonus
    # Credit". The prefix is not stable -- the same line has been read as "MEA"
    # -- and enumerating spellings of a provider's initials as aliases is a
    # losing game. Anchored at the end and bounded to a short prefix, so it
    # cannot quietly swallow an unrelated longer line.
    for rule in MAP:
        if rule.section is not section:
            continue
        for candidate in (rule.label, *rule.aliases):
            folded = normalize_label(candidate)
            if wanted.endswith(f" {folded}") and len(wanted) - len(folded) <= 5:
                return rule
    return None


def claimed_components() -> frozenset[tuple[Side, str]]:
    """Every (side, key) a rule claims.

    Side-aware on purpose. A key can exist on more than one side with different
    meanings -- ``delivery`` is both an export credit earned and, as
    ``applied:delivery``, the part of it spent this cycle. Comparing by name
    alone let a rule claiming one silently account for the other, so a genuinely
    unclaimed component reported as covered.
    """
    return frozenset(
        split_side(component, rule.side) for rule in MAP for component in rule.components
    )
