"""How close counts as agreement.

Every printed figure is rounded to the cent, so exact equality is the wrong test.
The allowance scales with the number of components behind a line rather than
being one global fudge factor: a line built by adding three components carries
three independent roundings, and a flat cent would fail it for arithmetic reasons
that say nothing about the rates. A report that cries wolf on its combined lines
is one nobody reads, which costs more than the precision gains.

The kWh allowance is separate and looser, because the sources genuinely differ:
Green Button rounds each interval to two decimals, which loses a little over a
month, and that is a property of the export rather than a defect.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Tolerance:
    #: Half a cent for the printed rounding, per component summed into the line.
    per_component: float = 0.01
    #: Proportional allowance, for the part that does scale with magnitude: the
    #: meter sources disagree slightly about which interval a kWh landed in, so a
    #: large line carries a little irreducible noise.
    #:
    #: Deliberately small. At 0.001 this allowed 18 cents on a $182 line, which
    #: was enough to hide a component assigned to the wrong printed line --
    #: Distribution "agreed" while carrying a charge that belonged to
    #: Transmission, and only Transmission's own shortfall gave it away. A
    #: tolerance wide enough to hide a whole component is not a tolerance.
    relative: float = 0.0005
    #: kWh agreement between two measurements of the same period.
    kwh_relative: float = 0.005
    #: A computed component smaller than this is not worth reporting as
    #: unmapped; it is rounding in the tariff, not a missing line.
    ignore_below: float = 0.005

    def line_ok(
        self, printed: float, computed: float, components: int = 1, gross: float | None = None
    ) -> bool:
        """Whether a printed and a computed amount agree.

        ``gross`` is the sum of the absolute values of the components behind the
        line, which is not the same as the line itself when they offset. The
        Conservation Incentive nets a $63.09 charge against a $28.87 credit and
        prints $34.25; the allocation noise rides on the $91.96 of energy priced,
        not on the $34.25 left after they cancel. Scaling by the printed figure
        there would hold a partly-cancelling line to a third of the precision the
        measurement can support.
        """
        scale = max(abs(printed), abs(gross) if gross is not None else 0.0)
        allowed = max(self.per_component * max(1, components), self.relative * scale)
        return abs(printed - computed) <= allowed

    def kwh_ok(self, left: float, right: float) -> bool:
        scale = max(abs(left), abs(right), 1.0)
        return abs(left - right) <= self.kwh_relative * scale
