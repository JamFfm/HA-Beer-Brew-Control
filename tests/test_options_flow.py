"""Options Flow (Design §8.1, §7.6)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import (
    DEFAULT_OPTIONS,
    HEATER,
    TWO_RESTS,
    FakeSwitch,
    NotificationLog,
    advance,
    controller_of,
    set_sensor,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er

from custom_components.brausteuerung.const import STATUS_RUNNING

STORED = {"recipe": TWO_RESTS, "recipe_name": "Helles", "library": [], "legacy_migrated": True}
HEATER2 = "switch.heater2"


async def _init(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    return result


async def test_live_update_without_reload(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Speichern übernimmt live; derselbe Controller bleibt (kein Reload)."""
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    result = await _init(hass, entry)
    suggested = {
        str(key): (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
    }
    assert suggested == DEFAULT_OPTIONS

    new = {**DEFAULT_OPTIONS, "hysteresis": 0.5, "safety_offset": 5.0, "language": "en"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], new)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == new
    assert entry.runtime_data is controller
    assert controller.loaded
    assert controller.hysteresis == 0.5
    assert controller.safety_offset == 5.0
    assert controller.language == "en"


@pytest.mark.parametrize(
    ("field", "value"),
    [("heater_entity", "light.x"), ("hysteresis", 0.05), ("hysteresis", 6), ("safety_offset", 21)],
)
async def test_selector_errors(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch, field: str, value: Any
) -> None:
    """Selektorfehler wie im Config Flow; Optionen bleiben unverändert."""
    entry = await setup_integration(stored=STORED)
    result = await _init(hass, entry)
    with pytest.raises(InvalidData) as exc:
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**DEFAULT_OPTIONS, field: value}
        )
    assert field in exc.value.schema_errors
    assert dict(entry.options) == DEFAULT_OPTIONS


async def test_registry_uuid_is_field_error(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Registry-UUID als Heizung → ``invalid_heater``."""
    entry = await setup_integration(stored=STORED)
    uuid = er.async_get(hass).async_get_or_create("switch", "test", "uid").id
    result = await _init(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**DEFAULT_OPTIONS, "heater_entity": uuid}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"heater_entity": "invalid_heater"}
    assert dict(entry.options) == DEFAULT_OPTIONS


async def test_settings_locked_while_running(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Sensor/Heizung bei running gesperrt (``base: settings_locked``), Regelparameter nicht."""
    set_sensor(hass, 40)
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    await controller.start()
    await hass.async_block_till_done()
    assert controller.status == STATUS_RUNNING

    for field, value in (("heater_entity", HEATER2), ("sensor_entity", "sensor.other")):
        result = await _init(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**DEFAULT_OPTIONS, field: value}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "settings_locked"}
    assert dict(entry.options) == DEFAULT_OPTIONS

    result = await _init(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**DEFAULT_OPTIONS, "hysteresis": 2.0}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert controller.hysteresis == 2.0
    assert controller.status == STATUS_RUNNING


async def test_heater_change_turns_off_both(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer: Any,
) -> None:
    """Heizungswechsel: OFF an alte und neue Heizung, Watchdog nur für die neue."""
    entry = await setup_integration(stored=STORED)
    # Alte Heizung wird manuell eingeschaltet und reagiert danach nicht mehr.
    hass.states.async_set(HEATER, "on")
    hass.states.async_set(HEATER2, "on")
    fake_switch.entity_modes[HEATER] = "stuck"
    await hass.async_block_till_done()
    fake_switch.reset()

    result = await _init(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**DEFAULT_OPTIONS, "heater_entity": HEATER2}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert controller_of(entry).heater_entity == HEATER2
    assert fake_switch.off_calls(HEATER) == 1
    assert fake_switch.off_calls(HEATER2) == 1
    assert hass.states.get(HEATER2).state == "off"

    # Die alte Heizung bleibt on: kein Verify-Retry, kein Aktoralarm.
    await advance(hass, freezer, 120)
    assert fake_switch.off_calls(HEATER) == 1
    assert fake_switch.off_calls(HEATER2) == 1
    assert "brausteuerung_actuator_failure" not in notifications


async def test_new_heater_watchdog_active(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer: Any,
) -> None:
    """Gegenprobe: Bleibt die NEUE Heizung on, greift der Watchdog für sie."""
    entry = await setup_integration(stored=STORED)
    hass.states.async_set(HEATER2, "on")
    fake_switch.entity_modes[HEATER2] = "stuck"
    await hass.async_block_till_done()
    fake_switch.reset()

    result = await _init(hass, entry)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**DEFAULT_OPTIONS, "heater_entity": HEATER2}
    )
    await hass.async_block_till_done()
    await advance(hass, freezer, 20)
    assert fake_switch.off_calls(HEATER2) >= 2
    assert fake_switch.off_calls(HEATER) == 1
    assert "brausteuerung_actuator_failure" in notifications
