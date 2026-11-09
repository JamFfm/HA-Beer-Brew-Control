"""Sicherheitsfunktionen (AK 13–15, 28; Design §7.2, §7.4, §7.5)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from conftest import (
    HEATER,
    SENSOR,
    TWO_RESTS,
    FakeSwitch,
    NotificationLog,
    advance,
    controller_of,
    set_sensor,
    ws_command,
)
from homeassistant.core import HassJob, HomeAssistant
from pytest_homeassistant_custom_component.common import get_scheduled_timer_handles

from custom_components.brausteuerung import logic
from custom_components.brausteuerung.const import (
    PHASE_HOLDING,
    STATUS_IDLE,
    STATUS_PAUSED,
    STATUS_RUNNING,
)
from custom_components.brausteuerung.controller import BrauController, BrauError

STORED = {"recipe": TWO_RESTS, "recipe_name": "Helles", "library": [], "legacy_migrated": True}


async def _running(
    hass: HomeAssistant, setup_integration, start_value: float = 40
) -> BrauController:
    set_sensor(hass, start_value)
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    await controller.start()
    await hass.async_block_till_done()
    return controller


def _heater(hass: HomeAssistant) -> str:
    return hass.states.get(HEATER).state


def _verify_timers(hass: HomeAssistant) -> list[Any]:
    """Aktive Verify-Timer des Aktor-Watchdogs."""
    return [
        handle
        for handle in get_scheduled_timer_handles(hass.loop)
        if not handle.cancelled()
        and any(
            isinstance(arg, HassJob) and getattr(arg.target, "__name__", "") == "_on_verify_off"
            for arg in (handle._args or ())
        )
    ]


# ---------------------------------------------------------------------------
# Übertemperatur (AK 13)
# ---------------------------------------------------------------------------


async def test_overtemp_in_heating(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer,
) -> None:
    """Aufheizphase: ist > soll + offset → OFF, paused, genau eine Benachrichtigung."""
    controller = await _running(hass, setup_integration)
    assert _heater(hass) == "on"
    set_sensor(hass, 65.14)
    await hass.async_block_till_done()
    assert controller.status == STATUS_PAUSED
    assert _heater(hass) == "off"
    assert controller.setpoint == 55.0
    assert notifications.count("brausteuerung_overtemp") == 1
    title, message = notifications.texts["brausteuerung_overtemp"]
    assert title == "🔥 Übertemperatur erkannt!"
    assert "(65.1 °C)" in message
    assert "(65.0 °C = Soll + Offset)" in message

    # Keine nachfolgende Entscheidung schaltet wieder ein.
    fake_switch.reset()
    for value in (66, 40, 30):
        set_sensor(hass, value)
        await hass.async_block_till_done()
    await advance(hass, freezer, 30)
    assert fake_switch.on_calls() == 0
    assert notifications.count("brausteuerung_overtemp") == 1


async def test_overtemp_in_holding(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
) -> None:
    """Haltephase: Übertemperatur bricht den Hold ab und pausiert."""
    controller = await _running(hass, setup_integration)
    set_sensor(hass, 55.0)
    await hass.async_block_till_done()
    assert controller.phase == PHASE_HOLDING
    set_sensor(hass, 53.0)
    await hass.async_block_till_done()
    assert _heater(hass) == "on"
    set_sensor(hass, 70.0)
    await hass.async_block_till_done()
    assert controller.status == STATUS_PAUSED
    assert controller.phase is None
    assert controller.hold_ends_at is None
    assert _heater(hass) == "off"
    assert notifications.count("brausteuerung_overtemp") == 1


async def test_overtemp_in_paused_new_episode(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer,
) -> None:
    """A1: In paused greift die Prüfung weiter; neue Episode → neue Benachrichtigung."""
    controller = await _running(hass, setup_integration)
    set_sensor(hass, 70.0)
    await hass.async_block_till_done()
    assert controller.status == STATUS_PAUSED
    # Episode endet
    set_sensor(hass, 50.0)
    await hass.async_block_till_done()
    # Manuell eingeschaltet: ohne Sicherheitsgrund bleibt das frei.
    hass.states.async_set(HEATER, "on")
    await advance(hass, freezer, 10)
    assert _heater(hass) == "on"

    fake_switch.reset()
    set_sensor(hass, 66.0)
    await hass.async_block_till_done()
    assert fake_switch.off_calls() == 1
    assert _heater(hass) == "off"
    assert controller.status == STATUS_PAUSED
    assert notifications.count("brausteuerung_overtemp") == 2


async def test_no_off_spam_during_overtemp(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer,
) -> None:
    """60 s Übertemperatur in paused, Heizung off → 1 OFF; manuell an → genau 1 weiteres."""
    controller = await _running(hass, setup_integration)
    fake_switch.reset()
    set_sensor(hass, 70.0)
    await hass.async_block_till_done()
    await advance(hass, freezer, 60)
    assert controller.status == STATUS_PAUSED
    assert fake_switch.off_calls() == 1

    hass.states.async_set(HEATER, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, 60)
    assert fake_switch.off_calls() == 2
    assert _heater(hass) == "off"
    assert notifications.count("brausteuerung_overtemp") == 1


# ---------------------------------------------------------------------------
# Kommunikationsverlust (AK 14)
# ---------------------------------------------------------------------------


async def test_comm_loss(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer,
) -> None:
    """unavailable: sofort OFF; nach 29 s running; nach 30 s paused + Benachrichtigung."""
    controller = await _running(hass, setup_integration)
    assert _heater(hass) == "on"
    hass.states.async_set(SENSOR, "unavailable")
    await hass.async_block_till_done()
    assert _heater(hass) == "off"
    assert controller.status == STATUS_RUNNING

    await advance(hass, freezer, 29, step=1)
    assert controller.status == STATUS_RUNNING
    assert "brausteuerung_comm_loss" not in notifications
    await advance(hass, freezer, 1, step=1)
    assert controller.status == STATUS_PAUSED
    assert notifications.count("brausteuerung_comm_loss") == 1
    title, message = notifications.texts["brausteuerung_comm_loss"]
    assert title == "📡 Kommunikationsverlust zum Temperatursensor!"
    assert f"({SENSOR})" in message


async def test_comm_loss_cancelled_by_valid_value(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer,
) -> None:
    """Ein gültiger Wert vor 30 s bricht den Watchdog ab."""
    controller = await _running(hass, setup_integration)
    hass.states.async_set(SENSOR, "unknown")
    await hass.async_block_till_done()
    await advance(hass, freezer, 20, step=1)
    set_sensor(hass, 41)
    await hass.async_block_till_done()
    assert _heater(hass) == "on"
    await advance(hass, freezer, 60, step=1)
    assert controller.status == STATUS_RUNNING
    assert "brausteuerung_comm_loss" not in notifications


async def test_invalid_sensor_in_paused_only_off_if_on(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer,
) -> None:
    """Ungültiger Wert in paused: OFF nur bei eingeschalteter Heizung, keine Benachrichtigung."""
    controller = await _running(hass, setup_integration)
    set_sensor(hass, 70)
    await hass.async_block_till_done()
    set_sensor(hass, 50)
    await hass.async_block_till_done()
    assert controller.status == STATUS_PAUSED
    fake_switch.reset()
    hass.states.async_set(SENSOR, "unavailable")
    await hass.async_block_till_done()
    assert fake_switch.off_calls() == 0
    hass.states.async_set(HEATER, "on")
    await advance(hass, freezer, 60)
    assert fake_switch.off_calls() == 1
    assert "brausteuerung_comm_loss" not in notifications


# ---------------------------------------------------------------------------
# Fail-safe bei Ausnahmen (AK 15)
# ---------------------------------------------------------------------------


async def test_exception_in_sensor_callback(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
) -> None:
    """Injizierte Ausnahme in der Regelverarbeitung → OFF + paused."""
    controller = await _running(hass, setup_integration)
    assert _heater(hass) == "on"
    fake_switch.reset()
    with patch.object(logic, "evaluate", side_effect=RuntimeError("Test")):
        set_sensor(hass, 41)
        await hass.async_block_till_done()
    assert fake_switch.off_calls() == 1
    assert controller.status == STATUS_PAUSED
    assert "brausteuerung_internal_error" in notifications


async def test_exception_during_start(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Ausnahme beim Start (außerhalb ``_evaluate``) → ``internal_error``, OFF, paused."""
    set_sensor(hass, 40)
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    fake_switch.reset()
    with (
        patch.object(controller, "_evaluate", side_effect=RuntimeError("Test")),
        pytest.raises(BrauError) as err,
    ):
        await controller.start()
    await hass.async_block_till_done()
    assert err.value.code == "internal_error"
    assert controller.status == STATUS_PAUSED
    assert fake_switch.off_calls() == 1


async def test_exception_during_ws_start(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch, hass_ws_client
) -> None:
    """Injizierte Ausnahme bei ``control/start`` → OFF, paused, WS-Fehler ``internal_error``."""
    set_sensor(hass, 40)
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    client = await hass_ws_client(hass)
    fake_switch.reset()
    with patch.object(controller, "_evaluate", side_effect=RuntimeError("Test")):
        response = await ws_command(client, "control/start")
    await hass.async_block_till_done()
    assert not response["success"]
    assert response["error"]["code"] == "internal_error"
    assert controller.status == STATUS_PAUSED
    assert fake_switch.off_calls() == 1


async def test_forced_off_with_invalid_reason_is_rejected(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """``force=True`` nur mit OFF und zulässigem Grund, sonst ``ValueError``."""
    entry = await setup_integration()
    controller = controller_of(entry)
    with pytest.raises(ValueError):
        controller._request_heater(False, "tick", force=True)
    with pytest.raises(ValueError):
        controller._request_heater(True, "stop", force=True)


async def test_failing_turn_on_turns_off(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
) -> None:
    """Fehlschlagender ``turn_on`` → OFF und Pause."""
    fake_switch.service_modes["turn_on"] = "raise"
    controller = await _running(hass, setup_integration)
    assert fake_switch.on_calls() == 1
    assert controller.status == STATUS_PAUSED
    assert fake_switch.calls[-1] == ("turn_off", HEATER)


async def test_failing_turn_off_while_running_pauses(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
) -> None:
    """Scheitert ein reguläres OFF während running → Pause."""
    controller = await _running(hass, setup_integration)
    fake_switch.service_modes["turn_off"] = "raise"
    set_sensor(hass, 55.0)
    await hass.async_block_till_done()
    assert controller.status == STATUS_PAUSED


# ---------------------------------------------------------------------------
# Veralteter Sensorwert (S5, AK 28)
# ---------------------------------------------------------------------------


async def test_stale_sensor_triggers(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer,
) -> None:
    """Heizung ≥ 10 min an und kein Sensorbericht seit 10 min → Pause + Benachrichtigung."""
    controller = await _running(hass, setup_integration)
    assert _heater(hass) == "on"
    await advance(hass, freezer, 9 * 60 + 50)
    assert controller.status == STATUS_RUNNING
    await advance(hass, freezer, 15)
    assert controller.status == STATUS_PAUSED
    assert _heater(hass) == "off"
    assert notifications.count("brausteuerung_stale_sensor") == 1


async def test_stale_sensor_not_triggered_with_heater_off(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer,
) -> None:
    """Haltephase mit ausgeschalteter Heizung: stabiler Wert löst nie aus."""
    controller = await _running(hass, setup_integration)
    set_sensor(hass, 55.0)
    await hass.async_block_till_done()
    assert _heater(hass) == "off"
    await advance(hass, freezer, 14 * 60, step=30)
    assert controller.status == STATUS_RUNNING
    assert "brausteuerung_stale_sensor" not in notifications


async def test_stale_sensor_not_triggered_with_fresh_reports(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer,
) -> None:
    """Heizung an, Sensor meldet (unveränderten Wert) regelmäßig → kein Alarm."""
    controller = await _running(hass, setup_integration)
    for _ in range(15):
        await advance(hass, freezer, 60, step=30)
        set_sensor(hass, 40)
        await hass.async_block_till_done()
    assert _heater(hass) == "on"
    assert controller.status == STATUS_RUNNING
    assert "brausteuerung_stale_sensor" not in notifications


# ---------------------------------------------------------------------------
# Aktor-Watchdog (S6, AK 28)
# ---------------------------------------------------------------------------


async def test_actuator_watchdog_stuck(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """stuck → Retry + genau eine Benachrichtigung; nach 3 Retries kein weiterer Aufruf."""
    hass.states.async_set(HEATER, "on")
    fake_switch.mode = "stuck"
    entry = await setup_integration()
    controller = controller_of(entry)
    assert fake_switch.off_calls() == 1
    assert len(_verify_timers(hass)) == 1

    await advance(hass, freezer, 15)
    assert fake_switch.off_calls() == 2
    assert notifications.count("brausteuerung_actuator_failure") == 1
    assert len(_verify_timers(hass)) == 1

    await advance(hass, freezer, 120)
    # Initial-OFF + 3 Retries, danach nichts mehr
    assert fake_switch.off_calls() == 4
    assert notifications.count("brausteuerung_actuator_failure") == 1
    assert caplog.text.count("schaltet nach 3 Wiederholungen nicht ab") == 1
    assert _verify_timers(hass) == []

    # Beobachtetes off setzt den Zähler zurück.
    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    assert controller._off_retry_count == 0


async def test_single_verify_timer(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Mehrere OFF-Befehle hintereinander → höchstens ein Verify-Timer."""
    entry = await setup_integration()
    controller = controller_of(entry)
    for _ in range(3):
        await controller.stop()
        await hass.async_block_till_done()
    assert fake_switch.off_calls() == 4
    assert len(_verify_timers(hass)) == 1


async def test_manual_on_after_off_is_no_alarm(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer,
) -> None:
    """Bewusstes manuelles Einschalten nach dem OFF löst keinen Watchdog aus."""
    entry = await setup_integration()
    controller = controller_of(entry)
    await controller.stop()
    await hass.async_block_till_done()
    fake_switch.reset()
    await advance(hass, freezer, 5)
    hass.states.async_set(HEATER, "on")
    await advance(hass, freezer, 60)
    assert fake_switch.off_calls() == 0
    assert "brausteuerung_actuator_failure" not in notifications
    assert controller.status == STATUS_IDLE


async def test_watchdog_retry_ends_initial_off_pending(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch, freezer
) -> None:
    """Review R4-4: Initial-OFF zugestellt, Heizung bleibt an → Retry beendet pending; 2 Aufrufe."""
    hass.states.async_set(HEATER, "on")
    fake_switch.queue = ["stuck"]
    entry = await setup_integration()
    controller = controller_of(entry)
    assert fake_switch.off_calls() == 1
    await advance(hass, freezer, 15)
    assert fake_switch.off_calls() == 2
    assert _heater(hass) == "off"
    assert controller.initial_off_pending is False
    await advance(hass, freezer, 120)
    assert fake_switch.off_calls() == 2


async def test_watchdog_retry_after_failed_initial_off(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch, freezer
) -> None:
    """Review R4-4: Initial-OFF scheitert, Watchdog-Retry gelingt → kein weiterer Startup-OFF."""
    hass.states.async_set(HEATER, "on")
    fake_switch.queue = ["raise"]
    entry = await setup_integration()
    controller = controller_of(entry)
    assert fake_switch.off_calls() == 1
    assert controller.initial_off_pending is True
    await advance(hass, freezer, 15)
    assert fake_switch.off_calls() == 2
    assert controller.initial_off_pending is False
    await advance(hass, freezer, 120)
    assert fake_switch.off_calls() == 2


async def test_actuator_unavailable_while_running(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer,
) -> None:
    """Heizung während running ≥ 30 s unavailable → paused + Benachrichtigung."""
    controller = await _running(hass, setup_integration)
    # Der Fake soll den unavailable-State nicht durch ein turn_on „reparieren“.
    fake_switch.mode = "stuck"
    hass.states.async_set(HEATER, "unavailable")
    await hass.async_block_till_done()
    await advance(hass, freezer, 29, step=1)
    assert controller.status == STATUS_RUNNING
    await advance(hass, freezer, 1, step=1)
    assert controller.status == STATUS_PAUSED
    assert notifications.count("brausteuerung_actuator_unavailable") == 1


async def test_actuator_unavailable_recovers(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: NotificationLog,
    freezer,
) -> None:
    """Ein gültiger State vor Ablauf bricht den Aktor-Watchdog ab."""
    controller = await _running(hass, setup_integration)
    hass.states.async_set(HEATER, "unavailable")
    await hass.async_block_till_done()
    await advance(hass, freezer, 20, step=1)
    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 60, step=1)
    assert controller.status == STATUS_RUNNING
    assert "brausteuerung_actuator_unavailable" not in notifications
