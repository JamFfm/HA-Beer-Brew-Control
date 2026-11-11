"""Entitäten (Design §8.5, §13.1)."""

from __future__ import annotations

import pytest
from conftest import (
    HEATER,
    SENSOR,
    TWO_RESTS,
    FakeSwitch,
    controller_of,
    set_sensor,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType

from custom_components.brausteuerung.const import (
    DOMAIN,
    STATUS_IDLE,
    STATUS_RUNNING,
)

STORED = {"recipe": TWO_RESTS, "recipe_name": "Helles", "library": [], "legacy_migrated": True}

ENTITIES = {
    "sensor": (
        "status",
        "current_rest",
        "target_temperature",
        "safety_threshold",
        "hold_end",
        "brew_started",
    ),
    "number": ("hysteresis", "safety_offset"),
    "select": ("language", "unit"),
    "button": ("start", "stop", "next_rest"),
}


def _eid(hass: HomeAssistant, entry: ConfigEntry, platform: str, key: str) -> str:
    """Entity-ID über die unique_id."""
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id is not None, key
    return entity_id


async def test_unique_ids_and_device(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Alle 13 Entitäten mit unique_id ``{entry_id}_{key}`` an einem Service-Gerät."""
    entry = await setup_integration(stored=STORED)
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(entries) == sum(len(keys) for keys in ENTITIES.values())
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert device is not None
    assert device.name == "HA-Beer-Brew-Control"
    assert device.manufacturer == "HA-Beer-Brew-Control"
    assert device.model == "Maischesteuerung"
    assert device.sw_version == "3.0.3"
    assert device.entry_type is DeviceEntryType.SERVICE
    for platform, keys in ENTITIES.items():
        for key in keys:
            entity = registry.async_get(_eid(hass, entry, platform, key))
            assert entity.unique_id == f"{entry.entry_id}_{key}"
            assert entity.device_id == device.id
            assert entity.translation_key == key
            assert entity.has_entity_name


async def test_entity_ids_stable(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Neuinstallation: Entity-IDs folgen der Domäne, nicht dem Gerätenamen."""
    entry = await setup_integration(stored=STORED)
    expected = {
        "sensor.brausteuerung_status",
        "sensor.brausteuerung_current_rest",
        "sensor.brausteuerung_target_temperature",
        "sensor.brausteuerung_safety_threshold",
        "sensor.brausteuerung_hold_end",
        "sensor.brausteuerung_brew_started",
        "button.brausteuerung_start",
        "button.brausteuerung_stop",
        "button.brausteuerung_next_rest",
        "select.brausteuerung_language",
        "select.brausteuerung_unit",
        "number.brausteuerung_hysteresis",
        "number.brausteuerung_safety_offset",
    }
    actual = {
        _eid(hass, entry, platform, key) for platform, keys in ENTITIES.items() for key in keys
    }
    assert actual == expected
    for entity_id in expected:
        assert hass.states.get(entity_id) is not None, entity_id


async def test_sensor_states_idle(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Zustände und Attribute im Leerlauf."""
    set_sensor(hass, 40)
    entry = await setup_integration(stored=STORED)
    status = hass.states.get(_eid(hass, entry, "sensor", "status"))
    assert status.state == STATUS_IDLE
    assert status.attributes["options"] == ["idle", "running", "paused", "done"]
    assert status.attributes["phase"] is None
    assert status.attributes["step_index"] == 0
    assert status.attributes["rest_count"] == 2
    current = hass.states.get(_eid(hass, entry, "sensor", "current_rest"))
    assert current.state == STATE_UNKNOWN
    assert current.attributes["index"] is None
    for key in ("target_temperature", "safety_threshold", "hold_end", "brew_started"):
        assert hass.states.get(_eid(hass, entry, "sensor", key)).state == STATE_UNKNOWN


async def test_sensor_states_running(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Running: Status, aktive Rast, Soll, Schwelle (2 Stellen), Startzeit, Hold-Ende."""
    set_sensor(hass, 40)
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    await controller.start()
    await hass.async_block_till_done()

    status = hass.states.get(_eid(hass, entry, "sensor", "status"))
    assert status.state == STATUS_RUNNING
    assert status.attributes["phase"] == "heating"
    current = hass.states.get(_eid(hass, entry, "sensor", "current_rest"))
    assert current.state == "Eiweißrast"
    assert current.attributes["index"] == 0
    target = hass.states.get(_eid(hass, entry, "sensor", "target_temperature"))
    assert float(target.state) == 55.0
    assert target.attributes["unit_of_measurement"] == "°C"
    assert "device_class" not in target.attributes
    assert "state_class" not in target.attributes
    threshold = hass.states.get(_eid(hass, entry, "sensor", "safety_threshold"))
    assert float(threshold.state) == 65.0
    assert "device_class" not in threshold.attributes
    started = hass.states.get(_eid(hass, entry, "sensor", "brew_started"))
    assert started.attributes["device_class"] == "timestamp"
    assert started.state not in (STATE_UNKNOWN, None)

    # Haltephase → Hold-Ende als Timestamp.
    set_sensor(hass, 55.5)
    await hass.async_block_till_done()
    hold_end = hass.states.get(_eid(hass, entry, "sensor", "hold_end"))
    assert hold_end.attributes["device_class"] == "timestamp"
    # Timestamp-Sensoren speichern ohne Mikrosekunden.
    assert hold_end.state == controller.hold_ends_at.replace(microsecond=0).isoformat()

    # Zweite Rast über next_rest.
    await controller.next_rest()
    await hass.async_block_till_done()
    current = hass.states.get(_eid(hass, entry, "sensor", "current_rest"))
    assert current.state == "Verzuckerung"
    assert current.attributes["index"] == 1


async def test_safety_threshold_rounded(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Die Schwelle wird auf zwei Stellen gerundet."""
    set_sensor(hass, 40)
    stored = {**STORED, "recipe": [{"name": "A", "temperature": 55.123, "duration": 5}]}
    entry = await setup_integration(stored=stored)
    await controller_of(entry).start()
    await hass.async_block_till_done()
    threshold = hass.states.get(_eid(hass, entry, "sensor", "safety_threshold"))
    assert threshold.state == "65.12"


async def test_unit_from_source_sensor(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Einheit vom Quellsensor; ohne Einheit dort die Anzeigeeinheit."""
    set_sensor(hass, 104, unit="°F")
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    await controller.start()
    await hass.async_block_till_done()
    target_id = _eid(hass, entry, "sensor", "target_temperature")
    assert hass.states.get(target_id).attributes["unit_of_measurement"] == "°F"

    hass.states.async_set(SENSOR, "40")
    await controller.async_update_settings(unit="°F")
    await hass.async_block_till_done()
    assert hass.states.get(target_id).attributes["unit_of_measurement"] == "°F"
    await controller.async_update_settings(unit="°C")
    await hass.async_block_till_done()
    assert hass.states.get(target_id).attributes["unit_of_measurement"] == "°C"


async def test_snapshot_setpoint_entity(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """``snapshot()['entities']['setpoint']`` ist die Entity-ID des Soll-Sensors."""
    entry = await setup_integration(stored=STORED)
    snap = controller_of(entry).snapshot()
    assert snap["entities"]["setpoint"] == _eid(hass, entry, "sensor", "target_temperature")


async def test_buttons(hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch) -> None:
    """Start, Nächste Rast, Stopp; Fehler als ServiceValidationError."""
    set_sensor(hass, 40)
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)

    async def press(key: str) -> None:
        await hass.services.async_call(
            "button",
            "press",
            {ATTR_ENTITY_ID: _eid(hass, entry, "button", key)},
            blocking=True,
        )

    with pytest.raises(ServiceValidationError) as err:
        await press("next_rest")
    assert err.value.translation_key == "not_running"

    await press("start")
    assert controller.status == STATUS_RUNNING
    await press("next_rest")
    assert controller.step_index == 1
    fake_switch.reset()
    await press("stop")
    await hass.async_block_till_done()
    assert controller.status == STATUS_IDLE
    assert fake_switch.off_calls(HEATER) == 1


async def test_selects(hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch) -> None:
    """Sprache und Einheit: Optionen, Zustand, Übernahme in die Optionen."""
    entry = await setup_integration(stored=STORED)
    language_id = _eid(hass, entry, "select", "language")
    unit_id = _eid(hass, entry, "select", "unit")
    language = hass.states.get(language_id)
    assert language.state == "de"
    assert language.attributes["options"] == ["en", "de"]
    assert hass.states.get(unit_id).attributes["options"] == ["°C", "°F"]

    await hass.services.async_call(
        "select", "select_option", {ATTR_ENTITY_ID: language_id, "option": "en"}, blocking=True
    )
    await hass.services.async_call(
        "select", "select_option", {ATTR_ENTITY_ID: unit_id, "option": "°F"}, blocking=True
    )
    await hass.async_block_till_done()
    assert entry.options["language"] == "en"
    assert entry.options["unit"] == "°F"
    assert hass.states.get(language_id).state == "en"
    assert hass.states.get(unit_id).state == "°F"


async def test_numbers(hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch) -> None:
    """Hysterese/Offset: Grenzen, Modus box, Übernahme ohne Benutzer-Kontext."""
    entry = await setup_integration(stored=STORED)
    hysteresis_id = _eid(hass, entry, "number", "hysteresis")
    offset_id = _eid(hass, entry, "number", "safety_offset")
    hysteresis = hass.states.get(hysteresis_id)
    assert float(hysteresis.state) == 1.0
    assert hysteresis.attributes["min"] == 0.1
    assert hysteresis.attributes["max"] == 5.0
    assert hysteresis.attributes["step"] == 0.1
    assert hysteresis.attributes["mode"] == "box"
    offset = hass.states.get(offset_id)
    assert offset.attributes["min"] == 0.0
    assert offset.attributes["max"] == 20.0
    assert offset.attributes["step"] == 0.5
    entity = er.async_get(hass).async_get(offset_id)
    assert entity.entity_category == "config"

    await hass.services.async_call(
        "number", "set_value", {ATTR_ENTITY_ID: hysteresis_id, "value": 0.5}, blocking=True
    )
    await hass.services.async_call(
        "number", "set_value", {ATTR_ENTITY_ID: offset_id, "value": 7.5}, blocking=True
    )
    await hass.async_block_till_done()
    controller = controller_of(entry)
    assert controller.hysteresis == 0.5
    assert controller.safety_offset == 7.5
    assert entry.options["hysteresis"] == 0.5
    assert float(hass.states.get(hysteresis_id).state) == 0.5

    # Außerhalb der Grenzen lehnt HA den Wert ab; der alte bleibt.
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "number", "set_value", {ATTR_ENTITY_ID: hysteresis_id, "value": 0.05}, blocking=True
        )
    assert controller.hysteresis == 0.5


async def test_entities_unavailable_after_unload(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Nach dem Entladen sind die Entitäten nicht mehr verfügbar."""
    entry = await setup_integration(stored=STORED)
    status_id = _eid(hass, entry, "sensor", "status")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(status_id).state == "unavailable"
