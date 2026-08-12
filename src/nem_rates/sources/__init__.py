"""Interval-data sources.

Everything here turns some external record of metered energy into
``IntervalReading`` objects for :mod:`nem_rates.billing`. Sources may pull in
dependencies -- a WebSocket client, an HTTP client -- which is why they live
outside the billing package, which stays stdlib-only.

Three sources, with different strengths:

============= ===================== ============================ ===========
source        reads                 totals                       history
============= ===================== ============================ ===========
CSV           PG&E's own export     2 dp per interval, ~2% low    as supplied
homeassistant long-term statistics  pre-aggregated buckets        ~5 months
influx        raw counter samples   exact (endpoint difference)   ~14 months
============= ===================== ============================ ===========

Totals are not the whole story. Both live sources see the same counter, so they
agree on how much energy moved; they disagree on *when*, and time-of-use pricing
cares. Home Assistant credits an advance to the bucket holding the later sample,
which biases energy forward over every boundary it spans; ``influx`` works from
the raw samples and can spread it pro rata instead. Measured against a real
statement that is worth more than the totals are -- see
:mod:`nem_rates.sources.influx`.

CSV is not in this package: it has no dependency to isolate, and it ships with
billing.
"""

from .homeassistant import (
    DEFAULT_EXPORT_ENTITY,
    DEFAULT_IMPORT_ENTITY,
    HaSettings,
    describe_resolution,
    load_dotenv,
    read_statistics,
    read_statistics_async,
)
from .influx import InfluxSettings, monotonic, read_counters

__all__ = [
    "DEFAULT_EXPORT_ENTITY",
    "DEFAULT_IMPORT_ENTITY",
    "HaSettings",
    "InfluxSettings",
    "describe_resolution",
    "load_dotenv",
    "monotonic",
    "read_counters",
    "read_statistics",
    "read_statistics_async",
]
