"""Regelung und Ablauf (AK 11, 12, 17–20; Design §7.2, §7.3)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from conftest import (
    DEFAULT_OPTIONS,
    HEATER,
    SENSOR,
    TWO_RESTS,
    FakeSwitch,
    advance,
    controller_of,
    set_sensor,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.brausteuerung.const import (
    CONF_SENSOR_ENTITY,
    PHASE_HEATING,
    PHASE_HOLDING,
    STATUS_DONE,
    STATUS_IDLE,
    STATUS_RUNNING,
)
from custom_components.brausteuerung.controller import BrauController, BrauError

STORED = {"recipe": TWO_RESTS, "recipe_name": "Helles", "library": [], "legacy_migrated": True}
ONE_REST_65 = {
    "recipe": [{"name": "Halten", "temperature": 65.0, "duration": 600}],
    "recipe_name": "",
    "library": [],
    "legacy_migrated": True,
}


async def _running(
    hass: HomeAssistant, setup_integration, stored: dict[str, Any], start_value: float = 40
) -> BrauController:
    set_sensor(hass, start_value)
    entry = await setup_integration(stored=stored)
    controller = controller_of(entry)
    await controller.start()
    await hass.async_block_till_done()
    return controller


def _heater(hass: HomeAssistant) -> str:
    return hass.states.get(HEATER).state


async def test_heating_and_hold_start(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch, freezer
) -> None:
    """AK 12: ist < soll → AN; Erreichen von soll startet die Haltezeit mit Rastdauer."""
    controller = await _running(hass, setup_integration, STORED)
    assert controller.status == STATUS_RUNNING
    assert controller.step_index == 0
    assert controller.phase == PHASE_HEATING
    assert controller.setpoint == 55.0
    assert fake_switch.on_calls() == 1
    assert _heater(hass) == "on"

    set_sensor(hass, 55.0)
    await hass.async_block_till_done()
    assert controller.phase == PHASE_HOLDING
    assert controller.hold_ends_at == dt_util.utcnow() + timedelta(minutes=15)
    assert _heater(hass) == "off"
    assert controller.snapshot()["safety_threshold"] == 65.0


@pytest.mark.parametrize("prev_on", [True, False])
async def test_hysteresis(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch, prev_on: bool
) -> None:
    """AK 11: soll 65, hyst 1: 63,9 → AN; 65,0 → AUS; 64,5 → Vorzustand bleibt."""
    controller = await _running(hass, setup_integration, ONE_REST_65)
    set_sensor(hass, 65.0)
    await hass.async_block_till_done()
    assert controller.phase == PHASE_HOLDING
    assert _heater(hass) == "off"

    set_sensor(hass, 63.9)
    await hass.async_block_till_done()
    assert _heater(hass) == "on"

    if not prev_on:
        set_sensor(hass, 65.0)
        await hass.async_block_till_done()
        assert _heater(hass) == "off"

    fake_switch.reset()
    set_sensor(hass, 64.5)
    await hass.async_block_till_done()
    assert _heater(hass) == ("on" if prev_on else "off")
    assert fake_switch.calls == []

    set_sensor(hass, 65.0)
    await hass.async_block_till_done()
    assert _heater(hass) == "off"


async def test_hold_expiry_and_brew_complete(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    freezer,
    notifications: list[str],
) -> None:
    """AK 20: Haltezeit-Ende → nächste Stufe; nach letzter Rast done, OFF, Benachrichtigung."""
    controller = await _running(hass, setup_integration, STORED)
    set_sensor(hass, 55.0)
    await hass.async_block_till_done()
    await advance(hass, freezer, 14 * 60, step=60)
    assert controller.step_index == 0
    await advance(hass, freezer, 60, step=60)
    assert controller.step_index == 1
    assert controller.phase == PHASE_HEATING
    assert controller.setpoint == 65.0
    assert _heater(hass) == "on"

    set_sensor(hass, 65.0)
    await hass.async_block_till_done()
    assert controller.phase == PHASE_HOLDING
    await advance(hass, freezer, 30 * 60, step=60)
    assert controller.status == STATUS_DONE
    assert controller.phase is None
    assert controller.setpoint is None
    assert controller.hold_ends_at is None
    assert _heater(hass) == "off"
    assert notifications.count("brausteuerung_brew_complete") == 1


async def test_next_rest_in_heating_phase(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """AK 19: Nächste Rast in der Aufheizphase → Index genau +1."""
    controller = await _running(hass, setup_integration, STORED)
    await controller.next_rest()
    await hass.async_block_till_done()
    assert controller.step_index == 1
    assert controller.phase == PHASE_HEATING
    assert controller.setpoint == 65.0


async def test_next_rest_in_holding_phase(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch, freezer
) -> None:
    """AK 19: In der Haltephase endet die Haltezeit, Index genau +1."""
    controller = await _running(hass, setup_integration, STORED)
    set_sensor(hass, 55.0)
    await hass.async_block_till_done()
    assert controller.phase == PHASE_HOLDING
    await controller.next_rest()
    await hass.async_block_till_done()
    assert controller.step_index == 1
    assert controller.phase == PHASE_HEATING
    assert controller.hold_ends_at is None
    # Der alte Hold-Timer ist abgemeldet: kein weiterer Sprung.
    await advance(hass, freezer, 16 * 60, step=60)
    assert controller.step_index == 1


async def test_next_rest_on_last_rest(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: list[str],
) -> None:
    """AK 19: Auf der letzten Rast → OFF, done, Benachrichtigung (manuell)."""
    controller = await _running(hass, setup_integration, STORED)
    await controller.next_rest()
    await hass.async_block_till_done()
    assert _heater(hass) == "on"
    await controller.next_rest()
    await hass.async_block_till_done()
    assert controller.status == STATUS_DONE
    assert _heater(hass) == "off"
    assert notifications.count("brausteuerung_brew_complete_manual") == 1


async def test_next_rest_outside_running(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """AK 19: Außerhalb running wirkungslos (``not_running``)."""
    set_sensor(hass, 40)
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    fake_switch.reset()
    with pytest.raises(BrauError) as err:
        await controller.next_rest()
    assert err.value.code == "not_running"
    assert controller.status == STATUS_IDLE
    assert controller.step_index == 0
    assert fake_switch.calls == []


@pytest.mark.parametrize(
    ("stored", "sensor", "heater", "options", "code"),
    [
        ({**STORED, "recipe": []}, "40", "off", None, "recipe_empty"),
        (STORED, "unavailable", "off", None, "sensor_invalid"),
        (STORED, "abc", "off", None, "sensor_invalid"),
        (STORED, "40", "unavailable", None, "heater_unavailable"),
        (
            STORED,
            "40",
            "off",
            {k: v for k, v in DEFAULT_OPTIONS.items() if k != CONF_SENSOR_ENTITY},
            "not_configured",
        ),
    ],
)
async def test_start_rejected(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    stored: dict[str, Any],
    sensor: str,
    heater: str,
    options: dict[str, Any] | None,
    code: str,
) -> None:
    """AK 17: Start wird bei leerem Rezept, ungültigem Sensor, fehlender Konfiguration abgelehnt."""
    hass.states.async_set(SENSOR, sensor)
    hass.states.async_set(HEATER, heater)
    entry = await setup_integration(stored=stored, options=options)
    controller = controller_of(entry)
    with pytest.raises(BrauError) as err:
        await controller.start()
    assert err.value.code == code
    assert controller.status == STATUS_IDLE


async def test_start_twice_rejected(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Zweiter Start während running → ``already_running``."""
    controller = await _running(hass, setup_integration, STORED)
    with pytest.raises(BrauError) as err:
        await controller.start()
    assert err.value.code == "already_running"
    assert controller.status == STATUS_RUNNING


async def test_stop(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch, freezer
) -> None:
    """AK 18: Stop → Heizung aus, keine Haltezeit, idle."""
    controller = await _running(hass, setup_integration, STORED)
    set_sensor(hass, 54.5)
    await hass.async_block_till_done()
    set_sensor(hass, 55.0)
    await hass.async_block_till_done()
    assert controller.hold_ends_at is not None
    set_sensor(hass, 53.0)
    await hass.async_block_till_done()
    assert _heater(hass) == "on"

    await controller.stop()
    await hass.async_block_till_done()
    assert controller.status == STATUS_IDLE
    assert controller.hold_ends_at is None
    assert controller.phase is None
    assert controller.setpoint is None
    assert _heater(hass) == "off"
    # Kein Hold-Ablauf und keine Regelung mehr.
    fake_switch.reset()
    await advance(hass, freezer, 20 * 60, step=60)
    assert controller.status == STATUS_IDLE
    assert fake_switch.on_calls() == 0


async def test_manual_switching_is_overridden_by_tick(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch, freezer
) -> None:
    """Manuelles Ausschalten in running überschreibt der 5-s-Re-Check."""
    await _running(hass, setup_integration, STORED)
    assert _heater(hass) == "on"
    fake_switch.reset()
    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    # Der Heizungs-State-Change selbst regelt nicht.
    assert fake_switch.on_calls() == 0
    await advance(hass, freezer, 5)
    assert fake_switch.on_calls() == 1
    assert _heater(hass) == "on"
