# Embedding the library

Core install has **zero third-party dependencies** and makes no network calls.
Lookups are pure and O(1), so it is safe to call in a tight loop or inside an
async event loop without an executor.

```bash
pip install tariffkit
```

## Basics

```python
from tariffkit import RateEngine, Config

engine = RateEngine(Config.load())  # or Config() for bundled PG&E defaults

point = engine.price_now()
point.import_price.total  # $/kWh to draw from the grid
point.export_price.total  # $/kWh earned by exporting
point.spread  # export - import; positive favours exporting
```

Every public API takes **timezone-aware** datetimes. Naive ones raise rather
than being guessed at: a silent eight-hour error is worse than a traceback.

```python
from datetime import datetime
from tariffkit import PACIFIC

engine.price_at(datetime(2026, 9, 15, 19, tzinfo=PACIFIC))
```

## Forecasting

Both sides are static published tables, so `forecast` reads ahead in a real
schedule rather than predicting one. The horizon is bounded only by the data
(20 years).

```python
curve = engine.forecast(hours=48)

curve.best_export_hours(3)  # highest export credit, earliest first
curve.cheapest_import_hours(3)  # cheapest hours to charge
curve.peak_spread()  # single best hour to export
curve.to_dict()  # JSON-ready

for point in curve:
    print(point.start, point.import_price.total, point.export_price.total)
```

DST is handled by stepping in absolute time: the spring-forward day yields 23
distinct hours and the fall-back day 25. Every point spans exactly one hour of
absolute time, so `end` is contiguous with the next `start` across both
transitions.

## Interoperability

`tariffkit.interop` renders a curve into the shapes other energy management
systems already read, so neither Home Assistant nor a template layer has to do
the reshaping.

```python
from tariffkit.interop import forecast_lists, predbat_payload, resample
from tariffkit.timeutil import now_pacific

resample(curve, 30)  # tuple[PricePoint, ...] on 30-minute boundaries

forecast_lists(curve, since=now_pacific())
# {'load_cost_forecast': [0.55214, 0.55214, ...],
#  'prod_price_forecast': [...],
#  'prediction_horizon': 95}              EMHASS runtime parameters, dollars

predbat_payload(engine)
# {'import': {'raw_today': [{'from': ..., 'to': ..., 'rate': 55.214}, ...],
#             'raw_tomorrow': [...]},
#  'export': {...}}                       Predbat entity attributes, cents
```

Three things to know:

- **Predbat values are cents, EMHASS values are dollars.** Predbat assumes pence
  per kWh and several of its thresholds are tuned to that magnitude. The
  `from` / `to` / `rate` shape tells Predbat the values are already in that
  scale.
- **`predbat_payload` takes an engine, not a curve**, because Predbat's
  `raw_today` means a calendar day. It builds its own curve anchored to local
  midnight; handing it a forecast starting at the current hour would leave the
  morning missing.
- **EMHASS lists are positional**, matched to its slots by index rather than by
  timestamp, so pass `since` to drop already-elapsed slots. Both default to
  30-minute resolution, which is the `optimization_time_step` EMHASS ships with.
  `forecast_payload` gives the same data keyed by timestamp, for consumers that
  accept a mapping.

Resampling repeats each hourly price across its sub-slots rather than
interpolating. That is exact, not an approximation: the tariff assigns one rate
to the whole clock hour, and no finer data exists upstream.

## Check the flags

Prices carry provenance. Acting on a number without checking these is the main
way to get a wrong answer:

```python
e = point.export_price
e.complete  # False -> CCA generation unconfigured; delivery only, understated
e.locked  # False -> past your 9-year lock; illustrative only
e.exact  # False -> far-future year where PG&E's own hour labels drift
```

## Components

Totals decompose, which is what makes a bill reconcilable:

```python
point.import_price.components
# {'cca_generation': 0.11878, 'cca_cost_relief_credit': -0.0062,
#  'distribution': 0.15764, 'pcia': 0.03476, ...}

point.export_price.components
# {'generation': 0.59312, 'delivery': 0.00193, 'acc_plus': 0.0088}
```

That vocabulary is the tariff sheet's own, so the set of keys changes with the
schedule, the supplier and any discount. For charting, or anywhere a fixed set
of series is needed, roll it up into groups instead:

```python
point.import_price.grouped()
# {<ComponentGroup.GENERATION: 'generation'>: 0.15377,
#  <ComponentGroup.DISTRIBUTION: 'distribution'>: 0.16922,
#  <ComponentGroup.TRANSMISSION: 'transmission'>: 0.05104,
#  <ComponentGroup.SURCHARGES: 'surcharges'>: 0.01623,
#  <ComponentGroup.CREDITS: 'credits'>: 0.0,
#  <ComponentGroup.OTHER: 'other'>: 0.0}

point.export_price.grouped()  # generation, delivery, credits, other
```

The groups for a direction are fixed and sum back to that direction's total,
so they can be stacked against the price itself. `ComponentGroup.OTHER` is a
safety valve: an unrecognized component still counts toward the total instead
of being silently dropped. `tariffkit.components.split_components` gives the
same roll-up with each group's underlying tariff lines kept, and `group_of`
answers for a single component name. See
[Component breakdown](home-assistant.md#component-breakdown) for what each
group contains.

## Fixed charges are separate

```python
engine.daily_fixed_charge()  # 0.79343 USD/day
```

The Base Services Charge is **not** in the $/kWh figures. It does not vary with
consumption, so folding it in would corrupt any marginal decision about
importing, exporting, or self-consuming. Include it when modelling a monthly
bill; exclude it when deciding what to do with the next kWh.

## Marginal cost caveat

`import_price.total` is what an additional imported kWh costs. For a solar
customer that only applies in hours where you are **net importing**. In an hour
where you are exporting, consuming one more kWh costs you the export credit you
forgo (`export_price.total`), which can be several times smaller. Pick the
right side based on your live production and load.

## Provenance

```python
engine.describe()
# {'tariff_effective': '2026-06-01', 'tariff_advice_letter': '7921-E',
#  'export_vintage': 'NBT26', 'export_years': [2026, 2045],
#  'acc_plus': 0.0088, 'lock_end': '2035-06-02', ...}
```

Pass an offset-aware datetime to resolve effective-dated provenance for a
historical or future price rather than the current snapshot:

```python
engine.describe(moment)
```

## Errors

All inherit `TariffKitError`:

| | |
|---|---|
| `ConfigError` | Invalid or inconsistent configuration |
| `DataError` | Vendored data missing or does not cover the request |
| `OutOfRangeError` | Timestamp outside the vendored years (subclass of `DataError`) |

```python
from tariffkit.errors import TariffKitError

try:
    point = engine.price_at(moment)
except TariffKitError as exc:
    log.warning("no price available: %s", exc)
```

Note that requesting a date before the earliest vendored tariff sheet
(2026-06-01) raises rather than back-dating current rates onto an older billing
period.

## Async

Nothing here does I/O, so call it directly from async code:

```python
async def handler():
    return engine.price_now().to_dict()
```

The one exception is constructing `RateEngine`, which reads and decompresses the
vendored matrices on first use. Build it once at startup, not per request.
