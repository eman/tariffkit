"""Interval-data sources.

Everything here turns some external record of metered energy into
``IntervalReading`` objects for :mod:`tariffkit.billing`. Sources may pull in
dependencies -- a WebSocket client, an HTTP client -- which is why they live
outside the billing package, which stays stdlib-only.

Three sources, with different strengths:

============= ========================= ============================ ===========
source        reads                     totals                       history
============= ========================= ============================ ===========
greenbutton   PG&E's Green Button CSV   2 dp per interval, ~2% low    as supplied
homeassistant long-term statistics      pre-aggregated buckets        ~5 months
influx        raw counter samples       exact (endpoint difference)   ~14 months
============= ========================= ============================ ===========

Totals are not the whole story. Both live sources see the same counter, so they
agree on how much energy moved; they disagree on *when*, and time-of-use pricing
cares. Home Assistant credits an advance to the bucket holding the later sample,
which biases energy forward over every boundary it spans; ``influx`` works from
the raw samples and can spread it pro rata instead. Measured against a real
statement that is worth more than the totals are -- see
:mod:`tariffkit.sources.influx`. Green Button is the opposite trade: its totals
are the worst of the three and its timing the best, because it is the utility's
own record at true fifteen-minute metering.

``greenbutton`` is stdlib-only and so needs no extra, unlike its neighbours. It
lives here anyway: what a module reads is a better organising principle than
what it happens to import, and putting the one file source somewhere else made
it hard to find.
"""

from .greenbutton import GreenButtonLayout, read_green_button
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
from .pge import (
    PgeSession,
    PgeSettings,
    PortalError,
    parse_green_button,
    read_green_button_download,
)

__all__ = [
    "DEFAULT_EXPORT_ENTITY",
    "DEFAULT_IMPORT_ENTITY",
    "GreenButtonLayout",
    "HaSettings",
    "InfluxSettings",
    "PgeSession",
    "PgeSettings",
    "PortalError",
    "describe_resolution",
    "load_dotenv",
    "monotonic",
    "parse_green_button",
    "read_counters",
    "read_green_button",
    "read_green_button_download",
    "read_statistics",
    "read_statistics_async",
]
