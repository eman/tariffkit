"""Rolling unbundled tariff components up into a fixed, stackable vocabulary.

A price's ``components`` mapping speaks the tariff sheet's own language: fifteen
or so import lines on a bundled residential schedule, a different set once a CCA
supplies generation, and another line again when a CARE discount or a SmartRate
event applies. That is exactly the right level for reconciling a statement, and
exactly the wrong level for a chart -- a fifteen-band stack is unreadable, and
a consumer that made one series per line would watch series appear and vanish as
the account's configuration changed.

The groups below are a small closed vocabulary that every component maps into.
Two properties make them safe to draw as a stacked graph against the price
itself:

* the set of groups for a direction is fixed, so the series exist whether or not
  the account happens to pay that kind of charge this hour; and
* every component lands in exactly one group -- anything this table has not seen
  falls into :attr:`ComponentGroup.OTHER` rather than being dropped -- so the
  groups sum back to the price, within per-component rounding.

Grouping is a presentation, not a billing rule. ``tariffkit.billing.ledger``
classifies the same components again, differently and for a different purpose:
which export credits may offset which charges. Neither is derived from the
other, because being drawn in the same band of a chart and being offsettable by
the same credit bucket are unrelated questions.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum


class ComponentGroup(StrEnum):
    """A band in a stacked price chart."""

    #: Generation supply, whoever supplies it, plus the charges that follow the
    #: supply decision: the PCIA a departed customer pays, the bundled PCIA
    #: credit a PG&E-supplied one receives, a CCA's own cost relief credit, and
    #: a SmartRate event's high-price energy charge.
    GENERATION = "generation"
    #: Distribution, and the Conservation Incentive Adjustment that implements
    #: the baseline credit as a distribution-side rate spread.
    DISTRIBUTION = "distribution"
    #: Transmission, its rate adjustments, and reliability services.
    TRANSMISSION = "transmission"
    #: The export side's delivery component. PG&E publishes the export rate as
    #: exactly two components -- delivery, which every Solar Billing Plan
    #: customer earns, and generation, which only a bundled one does. Delivery
    #: is not published split into distribution and transmission, so it gets its
    #: own band rather than being apportioned across two on a guess.
    DELIVERY = "delivery"
    #: Non-bypassable charges and riders: public purpose programs, nuclear
    #: decommissioning, competition transition, energy cost recovery, the
    #: wildfire fund and hardening charges, new system generation, the recovery
    #: bond charge and its offsetting credit, and the CCA franchise fee.
    SURCHARGES = "surcharges"
    #: Discounts and incentives: CARE, FERA, Medical Baseline, and the ACC Plus
    #: adder on the export side.
    CREDITS = "credits"
    #: Anything this module has not classified. Normally zero; non-zero means a
    #: schedule grew a line and this table has not caught up.
    OTHER = "other"

    @property
    def label(self) -> str:
        """Human-readable name, for interfaces without their own translations."""
        match self:
            case ComponentGroup.GENERATION:
                return "Generation"
            case ComponentGroup.DISTRIBUTION:
                return "Distribution"
            case ComponentGroup.TRANSMISSION:
                return "Transmission"
            case ComponentGroup.DELIVERY:
                return "Delivery"
            case ComponentGroup.SURCHARGES:
                return "Surcharges"
            case ComponentGroup.CREDITS:
                return "Credits"
            case ComponentGroup.OTHER:
                return "Other"


#: The groups an import price is drawn from, in stack order: supply at the
#: bottom, then wires, then policy charges, then whatever reduces the bill.
IMPORT_GROUPS: tuple[ComponentGroup, ...] = (
    ComponentGroup.GENERATION,
    ComponentGroup.DISTRIBUTION,
    ComponentGroup.TRANSMISSION,
    ComponentGroup.SURCHARGES,
    ComponentGroup.CREDITS,
    ComponentGroup.OTHER,
)

#: The groups an export credit is drawn from. The ACC has no retail wires or
#: policy charges in it -- there is nothing to bill a customer who is exporting.
EXPORT_GROUPS: tuple[ComponentGroup, ...] = (
    ComponentGroup.GENERATION,
    ComponentGroup.DELIVERY,
    ComponentGroup.CREDITS,
    ComponentGroup.OTHER,
)

#: Component name -> group. Import and export names share one table because the
#: names that appear on both sides mean the same thing on both sides.
COMPONENT_GROUPS: dict[str, ComponentGroup] = {
    # Generation supply and the charges attached to who supplies it.
    "generation": ComponentGroup.GENERATION,
    "cca_generation": ComponentGroup.GENERATION,
    "cca_cost_relief_credit": ComponentGroup.GENERATION,
    "cca_solar_bonus": ComponentGroup.GENERATION,
    "bundled_pcia": ComponentGroup.GENERATION,
    "pcia": ComponentGroup.GENERATION,
    "smartrate_high_price": ComponentGroup.GENERATION,
    # Distribution.
    "distribution": ComponentGroup.DISTRIBUTION,
    "conservation_incentive_adjustment": ComponentGroup.DISTRIBUTION,
    # Transmission.
    "transmission": ComponentGroup.TRANSMISSION,
    "transmission_rate_adjustments": ComponentGroup.TRANSMISSION,
    "reliability_services": ComponentGroup.TRANSMISSION,
    # Export-side avoided delivery.
    "delivery": ComponentGroup.DELIVERY,
    # Non-bypassable charges and riders.
    "public_purpose_programs": ComponentGroup.SURCHARGES,
    "nuclear_decommissioning": ComponentGroup.SURCHARGES,
    "competition_transition_charges": ComponentGroup.SURCHARGES,
    "energy_cost_recovery": ComponentGroup.SURCHARGES,
    "wildfire_fund_charge": ComponentGroup.SURCHARGES,
    "wildfire_hardening": ComponentGroup.SURCHARGES,
    "new_system_generation": ComponentGroup.SURCHARGES,
    "recovery_bond_charge": ComponentGroup.SURCHARGES,
    # Paired with the charge above, which it currently offsets exactly. Keeping
    # the pair in one group means the band shows the net, rather than inflating
    # surcharges and inventing an equal and opposite credit.
    "recovery_bond_credit": ComponentGroup.SURCHARGES,
    "franchise_fee_surcharge": ComponentGroup.SURCHARGES,
    # Discounts and incentives.
    "medical_discount": ComponentGroup.CREDITS,
    "acc_plus": ComponentGroup.CREDITS,
    "cca_acc_plus": ComponentGroup.CREDITS,
    # Bill-level components. A marginal price never carries these -- a baseline
    # credit depends on cumulative usage, and a tax on a whole cycle's charges
    # -- but ``group_of`` is public, so a caller grouping a ``Bill``'s
    # breakdown gets the same answers rather than a pile of ``OTHER``. The Base
    # Services Charge is deliberately absent: it is a $/day amount, and there
    # is no band of a per-kWh chart it honestly belongs in.
    "energy_commission_tax": ComponentGroup.SURCHARGES,
    "baseline_credit": ComponentGroup.DISTRIBUTION,
    "smartrate_credit": ComponentGroup.GENERATION,
}

#: The discount component is named for the program applied (``care_discount``,
#: ``fera_discount``), so the suffix is matched rather than every program being
#: enumerated -- a new discount program should not silently land in ``OTHER``.
_DISCOUNT_SUFFIX = "_discount"


def group_of(name: str) -> ComponentGroup:
    """The group ``name`` belongs to, or ``OTHER`` if it is unrecognized."""
    group = COMPONENT_GROUPS.get(name)
    if group is not None:
        return group
    if name.endswith(_DISCOUNT_SUFFIX):
        return ComponentGroup.CREDITS
    return ComponentGroup.OTHER


def split_components(
    components: Mapping[str, float],
    groups: Iterable[ComponentGroup],
) -> dict[ComponentGroup, dict[str, float]]:
    """Bucket ``components`` by group, keeping each group's underlying lines.

    Every group in ``groups`` is present, empty if nothing landed in it, so a
    consumer builds the same series every time. A component whose group is not
    among ``groups`` is folded into ``OTHER`` instead of being dropped, which
    keeps the sum identity true even if a direction ever grows a line from a
    band it was not expected to have.
    """
    ordered = tuple(groups)
    buckets: dict[ComponentGroup, dict[str, float]] = {group: {} for group in ordered}
    for name, value in components.items():
        group = group_of(name)
        if group not in buckets:
            group = ComponentGroup.OTHER
            buckets.setdefault(group, {})
        buckets[group][name] = value
    return buckets


def group_components(
    components: Mapping[str, float],
    groups: Iterable[ComponentGroup],
) -> dict[ComponentGroup, float]:
    """``components`` summed per group, one entry per group in ``groups``."""
    return {
        group: round(float(sum(lines.values())), 6)
        for group, lines in split_components(components, groups).items()
    }
