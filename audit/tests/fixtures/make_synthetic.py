"""Build the synthetic statement fixture.

Run with ``python -m audit.tests.fixtures.make_synthetic`` after changing it.

The fixture stands in for a real statement, which cannot be committed: it would
carry a name, an address, an account number and a year of daily consumption. What
matters for the parser is the *layout*, and every quirk that once broke it is
reproduced here deliberately:

* a charge whose label prints on the row below it, marked with a lone dot
* the Base Services Charge, billed per day rather than per kWh, and folded into
  Distribution and Public Purpose Programs rather than printed on its own line
* the baseline allowance, which uses the same dot marker but states a quantity
  and no money
* a right-hand sidebar containing the words "Total Usage"
* a section total whose label wraps onto the next row
* an unbundled breakdown laid out beside unrelated prose
* two sub-period blocks, because the cycle spans a rate change

The numbers are internally consistent, which is what lets ``self_check`` be
tested for both passing and failing.
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).parent / "statements" / "synthetic_cca_ratechange.txt"


def row(left: str = "", amount: str = "", col: int = 0, side: str = "", sidecol: int = 118) -> str:
    line = " " * col + left
    if amount:
        line = line.ljust(96 - len(amount)) + amount
    if side:
        line = line.ljust(sidecol) + side
    return line if side else line.rstrip()


def block(
    start: str,
    end: str,
    peak_kwh: str,
    peak_rate: str,
    peak_amt: str,
    off_kwh: str,
    off_rate: str,
    off_amt: str,
    allow: str,
    days: str,
    bl_rate: str,
    bl_amt: str,
    gen: str,
    pcia: str,
    ffs: str,
) -> list[str]:
    return [
        f" {start} to {end}",
        row(f".{'':29}{allow}       kWh          ({days} days       20.0 kWh/day)"),
        row("Baseline Allowance", side="x", sidecol=100),
        row("Energy Charges"),
        row(
            f"  Peak                        {peak_kwh}        kWh        @   ${peak_rate}",
            peak_amt,
            side="To program your smart device, scan the",
        ),
        row(
            f"  Off Peak                    {off_kwh}        kWh        @   ${off_rate}",
            off_amt,
            side="QR code or enter the RIN code above and",
        ),
        row(
            f"Baseline Credit               {allow}        kWh        @   -${bl_rate}",
            bl_amt,
            side="follow the on-screen instructions.",
        ),
        row("Generation Credit", gen, side="Service Information"),
        row("Power Charge Indifference Adjustment", pcia),
        row("Franchise Fee Surcharge", ffs, side="Meter #                    99999999"),
    ]


PROSE = [
    "PG&E offers a monthly discount on electric bills for income-qualified",
    "households of three or more persons. To see if you qualify, please call",
    "1-800-PGE-5000 or apply online at www.pge.com/fera.",
    "Electric power line safety: PG&E cares about your safety. Be aware of",
    "your surroundings and keep yourself, tools, equipment and antennas at",
    "least 10 feet away from overhead power lines. If you see an electric",
    "power line fall to the ground, keep yourself and others away. Call 911.",
    "Visit www.pge.com/MyEnergy for a detailed bill comparison.",
]

#: Distribution and Public Purpose Programs each carry part of the $24.60 daily
#: Base Services Charge (14.60 and 10.00), which is why neither equals the
#: computed component of the same name and why the two are reconciled as a group.
BREAKDOWN = [
    ("Your Electric Charges Breakdown  (from page 2)", ""),
    ("Conservation Incentive", "$7.88"),
    ("Transmission", "20.00"),
    ("Distribution", "94.60"),
    ("Electric Public Purpose Programs", "22.00"),
    ("Nuclear Decommissioning", "-0.05"),
    ("Wildfire Fund Charge", "2.00"),
    ("Recovery Bond Charge", "3.00"),
    ("Recovery Bond Credit", "-3.00"),
    ("Wildfire Hardening Charge", "1.00"),
    ("Competition Transition Charges (CTC)", "0.15"),
    ("Energy Cost Recovery Amount", "0.02"),
    ("PCIA", "10.00"),
    ("Taxes and Other", "1.00"),
    ("Total Electric Charges", "$158.60"),
]


def build() -> str:
    page0 = "\n".join(
        [
            row(" ENERGY STATEMENT"),
            row(" Service For:", side="Your Account Summary", sidecol=80),
            row(" JANE Q CUSTOMER"),
            row(" 1 EXAMPLE ST"),
            "",
            " " * 80 + "Amount Due on Previous Statement".ljust(60) + "100.00",
            " " * 80 + "Payment(s) Received Since Last Statement".ljust(60) + "-100.00",
            " " * 80 + "Previous Unpaid Balance".ljust(60) + "0.00",
            " " * 80 + "Current PG&E Electric Delivery Charges".ljust(60) + "158.60",
            " " * 80 + "MCE Electric Generation Charges".ljust(60) + "136.70",
            " " * 80 + "Total Amount Due by 03/26/2026".ljust(60) + "$295.30",
            "",
            " Account Number:  9999999999-9   Due Date:  03/26/2026",
        ]
    )

    page2 = "\n".join(
        [
            row(" ENERGY STATEMENT", side="Account No:  9999999999-9", sidecol=100),
            row(" www.pge.com/MyEnergy", side="Statement Date:       02/05/2026", sidecol=100),
            " Details of PG&E Electric Delivery Charges",
            "12/30/2025 to 01/29/2026 (31 billing days)",
            "Service For:  1 EXAMPLE ST",
            "Rate Schedule:  Time-of-Use (Peak Pricing 4 - 9 p.m. Every Day)",
            row(".                             31        days       @   $0.79343", "$24.60"),
            row(
                "Base Services Charge", side="Baseline Territory                    X", sidecol=118
            ),
            *block(
                "12/30/2025",
                "12/31/2025",
                "20.000000",
                "0.50000",
                "$10.00",
                "50.000000",
                "0.40000",
                "20.00",
                "40.00",
                "2",
                "0.05000",
                "-2.00",
                "-5.00",
                "1.00",
                "0.10",
            ),
            # A sidebar total, far right. It must not close the section: doing so
            # drops every row below it.
            " " * 118 + "Total Usage                        1000.000",
            *block(
                "01/01/2026",
                "01/29/2026",
                "60.000000",
                "0.50000",
                "$30.00",
                "250.000000",
                "0.40000",
                "100.00",
                "290.00",
                "29",
                "0.03448",
                "-10.00",
                "-20.00",
                "9.00",
                "0.90",
            ),
            row("Total PG&E Electric Delivery Charges", "$158.60"),
            "2011 Vintaged Power Charge Indifference Adjustment",
            "   Electric Usage This Period: 1000.000000 kWh, 31 billing days",
        ]
    )

    page3 = "\n".join(
        [
            row(" ENERGY STATEMENT", side="Account No:  9999999999-9", sidecol=100),
            " Details of MCE Electric Generation Charges",
            "12/30/2025 to 01/29/2026 (31 billing days)",
            "Rate Schedule:       ETOUC",
            row(
                "Off Peak Winter               900.000000     kWh    @  $0.13500",
                "$121.50",
                side="USCA-XXMC-0000",
                sidecol=104,
            ),
            row(
                "Peak Winter                   100.000000     kWh    @  $0.14900",
                "14.90",
                side="www.pge.com/rin",
                sidecol=104,
            ),
            row("                                             Net Charges       136.40"),
            row("Energy Commission Tax", "0.30"),
            # The total's label wraps, leaving the amount on the row below.
            row("Total MCE Electric Generation", side="To program your smart device", sidecol=104),
            row("Charges", "$136.70", side="Service Information", sidecol=104),
        ]
    )

    page4 = [row(" ENERGY STATEMENT", side="Statement Date:       02/05/2026", sidecol=100)]
    for index in range(max(len(PROSE), len(BREAKDOWN))):
        left = PROSE[index] if index < len(PROSE) else ""
        line = (" " + left).ljust(78)
        if index < len(BREAKDOWN):
            label, amount = BREAKDOWN[index]
            line += "     " + label
            if amount:
                line = line.ljust(160 - len(amount)) + amount
        page4.append(line.rstrip())

    return "\n\x0c\n".join([page0, "", page2, page3, "\n".join(page4), ""])


if __name__ == "__main__":
    OUT.write_text(build(), encoding="utf-8")
    print(f"wrote {OUT}")
