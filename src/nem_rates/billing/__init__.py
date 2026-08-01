"""Bill computation from interval meter data.

Pure and dependency-free: readings in, decomposed charges out. See
``nem_rates.billing.engine`` for what is deliberately out of scope.
"""

from .engine import BillEngine, hourly
from .ingest import CsvLayout, read_csv
from .models import Bill, BillingPeriod, IntervalReading, UsageBucket
from .netting import check_coverage, find_gaps, find_overlaps, net_intervals

__all__ = [
    "Bill",
    "BillEngine",
    "BillingPeriod",
    "CsvLayout",
    "IntervalReading",
    "UsageBucket",
    "check_coverage",
    "find_gaps",
    "find_overlaps",
    "hourly",
    "net_intervals",
    "read_csv",
]
