"""Home Assistant MQTT Discovery payloads.

Publishing these lets Home Assistant create the sensors on its own, so the MQTT
path needs no custom component at all.
"""

from __future__ import annotations

from typing import Any

from ..models import Utility

DEVICE_ID = "tariffkit"

#: (object_id, name, state topic suffix, icon)
SENSORS: tuple[tuple[str, str, str, str], ...] = (
    ("import_price", "Import Price", "import_price", "mdi:transmission-tower-export"),
    ("export_price", "Export Price", "export_price", "mdi:transmission-tower-import"),
    ("spread", "Export Spread", "spread", "mdi:swap-vertical"),
)


def _device(engine_info: dict[str, Any]) -> dict[str, Any]:
    utility = Utility(engine_info.get("utility", Utility.PACIFIC_GAS_AND_ELECTRIC))
    return {
        "identifiers": [DEVICE_ID],
        "name": f"{utility.short_name} Rates",
        "manufacturer": utility.display_name,
        "model": f"{engine_info.get('tariff', 'E-ELEC')} / {engine_info.get('export_vintage', '')}",
        "sw_version": _version(),
    }


def _version() -> str:
    from .. import __version__

    return __version__


def discovery_payloads(
    engine_info: dict[str, Any],
    topic_prefix: str = "tariffkit",
    discovery_prefix: str = "homeassistant",
) -> list[tuple[str, dict[str, Any]]]:
    """Build ``(topic, payload)`` pairs to publish retained.

    Prices are $/kWh rather than a plain currency amount, so they are reported
    as plain measurements with a unit rather than ``device_class: monetary`` --
    Home Assistant rejects a monetary sensor whose unit is not a bare currency.
    """
    device = _device(engine_info)
    payloads: list[tuple[str, dict[str, Any]]] = []
    for object_id, name, suffix, icon in SENSORS:
        payloads.append(
            (
                f"{discovery_prefix}/sensor/{DEVICE_ID}/{object_id}/config",
                {
                    "name": name,
                    "unique_id": f"{DEVICE_ID}_{object_id}",
                    "object_id": f"{DEVICE_ID}_{object_id}",
                    "state_topic": f"{topic_prefix}/{suffix}",
                    "json_attributes_topic": f"{topic_prefix}/{suffix}/attributes",
                    "availability_topic": f"{topic_prefix}/status",
                    "unit_of_measurement": "USD/kWh",
                    "state_class": "measurement",
                    "suggested_display_precision": 5,
                    "icon": icon,
                    "device": device,
                },
            )
        )

    payloads.append(
        (
            f"{discovery_prefix}/sensor/{DEVICE_ID}/tou_period/config",
            {
                "name": "TOU Period",
                "unique_id": f"{DEVICE_ID}_tou_period",
                "object_id": f"{DEVICE_ID}_tou_period",
                "state_topic": f"{topic_prefix}/tou_period",
                "availability_topic": f"{topic_prefix}/status",
                "icon": "mdi:clock-outline",
                "device": device,
            },
        )
    )
    return payloads
