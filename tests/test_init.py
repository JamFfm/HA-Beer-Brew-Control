"""Lebenszyklus: Setup/Unload/Reload, Stop-Pfade, Initial-OFF, Shutdown (Design §7.1, §13.1)."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import Mock, patch

import pytest
from conftest import (
    DEFAULT_OPTIONS,
    HEATER,
    TWO_RESTS,
    FakeSwitch,
    advance,
    controller_of,
    set_sensor,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant

from custom_components.brausteuerung import controller as controller_module
from custom_components.brausteuerung import migration
from custom_components.brausteuerung.const import (
    DATA_ACTIVE,
    PHASE_HOLDING,
    STATUS_IDLE,
    STATUS_RUNNING,
)
from custom_components.brausteuerung.controller import BrauError

LOGGER_NAME = "custom_components.brausteuerung"

STORED = {
    "recipe": TWO_RESTS,
    "recipe_name": "Helles",
    "library": [{"name": "Helles", "steps": TWO_RESTS}],
    "legacy_migrated": True,
}


def _errors(caplog: pytest.LogCaptureFixture, prefix: str = LOGGER_NAME) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR and r.name.startswith(prefix)]


# ---------------------------------------------------------------------------
# Setup / Unload / Reload (AK 8, AK 15)
# ---------------------------------------------------------------------------


async def test_setup_unload_reload(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Setup, Reload und Unload laufen ohne Exception und senden jeweils OFF."""
    entry = await setup_integration()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[DATA_ACTIVE] == entry.entry_id
    # Initial-OFF bei State off: genau ein turn_off
    assert fake_switch.off_calls() == 1

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    # OFF beim Unload + Initial-OFF des neuen Controllers
    assert fake_switch.off_calls() == 3

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert fake_switch.off_calls() == 4
    assert DATA_ACTIVE not in hass.data


async def test_snapshot_contents(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """Snapshot enthält Rezept, Name, Bibliothek und Einstellungen."""
    entry = await setup_integration(stored=STORED)
    snap = controller_of(entry).snapshot()
    assert snap["loaded"] is True
    assert snap["version"] == "3.0.0"
    assert snap["status"] == STATUS_IDLE
    assert snap["recipe_name"] == "Helles"
    assert snap["recipe"] == TWO_RESTS
    assert snap["rest_count"] == 2
    assert snap["library"] == [{"name": "Helles", "steps": TWO_RESTS}]
    assert snap["settings"] == DEFAULT_OPTIONS
    assert snap["safety_threshold"] is None
    # Seit FEAT-003 gibt es den target_temperature-Sensor (Auflösung über unique_id).
    assert snap["entities"] == {"setpoint": "sensor.brausteuerung_target_temperature"}


async def test_listener_notified_and_removed(
    hass: HomeAssistant, setup_integration, fake_switch
) -> None:
    """Listener werden bei Änderungen benachrichtigt und lassen sich abmelden."""
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    calls: list[int] = []
    remove = controller.async_add_listener(lambda: calls.append(1))
    await controller.stop()
    assert calls
    remove()
    remove()  # mehrfach harmlos
    calls.clear()
    await controller.stop()
    assert calls == []


async def test_stage1_shutdown_job(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Stage 1: Der registrierte Shutdown-Job schaltet aus und setzt idle."""
    set_sensor(hass, 40)
    with patch.object(
        hass, "async_add_shutdown_job", wraps=hass.async_add_shutdown_job
    ) as mock_add:
        entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    await controller.start()
    await hass.async_block_till_done()
    assert controller.status == STATUS_RUNNING
    job = next(
        call.args[0]
        for call in mock_add.call_args_list
        if getattr(call.args[0].target, "__self__", None) is controller
    )
    fake_switch.reset()
    result = hass.async_run_hass_job(job)
    if result is not None:
        await result
    assert fake_switch.off_calls() == 1
    assert controller.status == STATUS_IDLE

    # Die Stop-Hooks bleiben registriert: Stage 2 sendet danach noch ein OFF.
    fake_switch.reset()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert fake_switch.off_calls() == 1


async def test_stage2_stop_event(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stage 2 (Review R4-1): STOP-Event mit normalem Setup inkl. realem http."""
    entry = await setup_integration()
    assert "http" in hass.config.components
    assert "websocket_api" in hass.config.components
    fake_switch.reset()
    caplog.clear()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert fake_switch.off_calls() == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert "Unable to remove unknown job listener" not in caplog.text
    assert not _errors(caplog, "homeassistant.components.http")


async def test_cancel_failure_does_not_block_off(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ein werfender Remover verhindert weder OFF noch erfolgreiches Unload."""
    entry = await setup_integration()
    controller = controller_of(entry)
    real_unsub = controller._sensor_unsub
    controller._sensor_unsub = Mock(side_effect=KeyError("weg"))
    fake_switch.reset()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert fake_switch.off_calls() == 1
    assert caplog.text.count("Abmelden von _sensor_unsub fehlgeschlagen") == 1
    assert real_unsub is not None
    real_unsub()


async def test_cancel_is_idempotent(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """``_async_cancel`` ruft ein Handle genau einmal auf."""
    entry = await setup_integration()
    controller = controller_of(entry)
    handle = Mock()
    controller._hold_unsub = handle
    controller._async_cancel("_hold_unsub")
    controller._async_cancel("_hold_unsub")
    handle.assert_called_once()
    assert controller._hold_unsub is None


async def test_shutdown_off_fails_while_running(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    notifications: list[str],
) -> None:
    """Scheiterndes Shutdown-OFF: keine Pause, keine Benachrichtigung, keine Folge-Task."""
    set_sensor(hass, 40)
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    await controller.start()
    await hass.async_block_till_done()
    assert controller.status == STATUS_RUNNING

    fake_switch.mode = "raise"
    fake_switch.reset()
    notifications.clear()
    await controller.async_shutdown("unload")
    assert fake_switch.off_calls() == 1
    assert not [n for n in notifications if n.startswith("brausteuerung_")]
    assert controller.pending_actuator_tasks == set()
    assert controller.status == STATUS_IDLE


async def test_setup_failure_after_start_turns_off(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Ausnahme nach ``async_start``: Shutdown mit OFF, DATA_ACTIVE entfernt."""
    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        side_effect=RuntimeError("Plattform kaputt"),
    ):
        entry = await setup_integration()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    # Initial-OFF + OFF aus dem Shutdown
    assert fake_switch.off_calls() == 2
    assert DATA_ACTIVE not in hass.data


async def test_unload_with_failing_platform_unload(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Fehlschlagendes ``async_unload_platforms``: OFF trotzdem, DATA_ACTIVE entfernt."""
    entry = await setup_integration()
    fake_switch.reset()
    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        side_effect=RuntimeError("Unload kaputt"),
    ):
        assert not await hass.config_entries.async_unload(entry.entry_id)
    assert fake_switch.off_calls() == 1
    assert DATA_ACTIVE not in hass.data


async def test_restart_without_auto_resume(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch, freezer
) -> None:
    """AK 16: Nach Neustart idle, keine Haltezeit, OFF, Daten unverändert."""
    set_sensor(hass, 40)
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    await controller.start()
    set_sensor(hass, 56)
    await hass.async_block_till_done()
    assert controller.phase == PHASE_HOLDING
    assert controller.hold_ends_at is not None
    fake_switch.reset()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restarted = controller_of(entry)
    assert restarted is not controller
    assert restarted.status == STATUS_IDLE
    assert restarted.hold_ends_at is None
    assert restarted.setpoint is None
    assert fake_switch.off_calls() >= 1
    snap = restarted.snapshot()
    assert snap["recipe"] == TWO_RESTS
    assert snap["recipe_name"] == "Helles"
    assert snap["library"] == STORED["library"]
    assert snap["settings"] == DEFAULT_OPTIONS
    # Auch nach Ablauf der alten Haltezeit läuft nichts weiter.
    fake_switch.reset()
    await advance(hass, freezer, 20 * 60, step=60)
    assert restarted.status == STATUS_IDLE
    assert fake_switch.on_calls() == 0


async def test_second_controller_is_rejected(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Guard: läuft schon ein Controller, scheitert das Setup ohne Aktorbefehl."""
    hass.data[DATA_ACTIVE] = "anderer_entry"
    entry = await setup_integration()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert fake_switch.calls == []
    assert hass.data.pop(DATA_ACTIVE) == "anderer_entry"


async def test_shutdown_blocks_operations(
    hass: HomeAssistant, setup_integration, fake_switch
) -> None:
    """Nach ``async_shutdown`` liefern alle Operationen ``not_loaded``."""
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    await controller.async_shutdown("unload")
    assert controller.loaded is False
    assert controller.snapshot() == {"loaded": False}
    operations = (
        controller.start,
        controller.stop,
        controller.next_rest,
        controller.clear_recipe,
        lambda: controller.add_rest(50, 10),
        lambda: controller.save_recipe("Neu"),
        lambda: controller.async_update_settings(hysteresis=2.0),
    )
    for operation in operations:
        with pytest.raises(BrauError) as err:
            await operation()
        assert err.value.code == "not_loaded"


async def test_shutdown_timeout(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """Hängender Aktor: ``async_shutdown`` kehrt nach OFF- plus Task-Timeout zurück."""
    entry = await setup_integration()
    controller = controller_of(entry)
    fake_switch.mode = "hang"
    # Hängende Stop-OFF-Task, auf die der Shutdown höchstens TASK_TIMEOUT wartet.
    await controller.stop()
    assert controller.pending_actuator_tasks
    with (
        patch.object(controller_module, "SHUTDOWN_OFF_TIMEOUT", 0.1),
        patch.object(controller_module, "SHUTDOWN_TASK_TIMEOUT", 0.1),
    ):
        started = hass.loop.time()
        await controller.async_shutdown("unload")
        elapsed = hass.loop.time() - started
    assert elapsed < 1.0
    fake_switch.release.set()
    await hass.async_block_till_done()
    assert controller.pending_actuator_tasks == set()


async def test_unload_while_turn_on_hangs(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Unload während ``turn_on`` hängt: ``turn_off`` ist der letzte Aktorbefehl."""
    set_sensor(hass, 40)
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    fake_switch.service_modes["turn_on"] = "hang"
    await controller.start()
    await asyncio.sleep(0)
    assert fake_switch.calls[-1] == ("turn_on", HEATER)

    hass.loop.call_later(0.05, fake_switch.release.set)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert fake_switch.calls[-1] == ("turn_off", HEATER)
    assert fake_switch.completed[-1] == ("turn_off", HEATER)
    assert ("turn_on", HEATER) in fake_switch.completed
    assert hass.states.get(HEATER).state == "off"


async def test_old_heater_off_task_is_awaited_on_unload(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Hängender OFF an die alte Heizung wird verfolgt und beim Unload abgewartet."""
    entry = await setup_integration()
    controller = controller_of(entry)
    hass.states.async_set("switch.heater2", "off")
    fake_switch.entity_modes[HEATER] = "hang"
    await controller.async_update_settings(heater_entity="switch.heater2")
    await asyncio.sleep(0)
    hanging = [task for task in controller.pending_actuator_tasks if not task.done()]
    assert len(hanging) == 1
    assert fake_switch.off_calls("switch.heater2") == 1

    hass.loop.call_later(0.05, fake_switch.release.set)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hanging[0].done()
    assert ("turn_off", HEATER) in fake_switch.completed


# ---------------------------------------------------------------------------
# Initial-OFF (S2, §7.1 Schritt 3, Review R4-2/R4-3)
# ---------------------------------------------------------------------------


async def test_initial_off_when_heater_already_off(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch, freezer
) -> None:
    """State schon off → trotzdem genau ein turn_off, danach keiner mehr."""
    entry = await setup_integration()
    assert fake_switch.off_calls() == 1
    assert controller_of(entry).initial_off_pending is False
    await advance(hass, freezer, 120)
    assert fake_switch.off_calls() == 1


@pytest.mark.parametrize("first_state", ["on", "off"])
async def test_initial_off_waits_for_heater_state(
    hass: HomeAssistant, setup_integration, freezer, first_state: str
) -> None:
    """Heizung erscheint erst nach dem Setup → kein Aufruf vorher, danach genau einer."""
    switch = FakeSwitch(hass)
    switch.register()
    entry = await setup_integration()
    await advance(hass, freezer, 30)
    assert switch.off_calls() == 0
    assert controller_of(entry).initial_off_pending is True

    hass.states.async_set(HEATER, first_state)
    await hass.async_block_till_done()
    assert switch.off_calls() == 1
    await advance(hass, freezer, 120)
    assert switch.off_calls() == 1
    assert controller_of(entry).initial_off_pending is False


async def test_initial_off_service_missing(
    hass: HomeAssistant,
    setup_integration,
    freezer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``ServiceNotFound``: kein error-Log, Versuch bei jedem Tick, später genau ein OFF."""
    hass.states.async_set(HEATER, "off")
    entry = await setup_integration()
    controller = controller_of(entry)
    assert controller.initial_off_pending is True
    with patch.object(controller, "_async_turn_off", wraps=controller._async_turn_off) as spy:
        await advance(hass, freezer, 20)
        assert spy.call_count == 4
    assert not _errors(caplog)

    switch = FakeSwitch(hass)
    switch.register()
    await advance(hass, freezer, 5)
    assert switch.off_calls() == 1
    await advance(hass, freezer, 120)
    assert switch.off_calls() == 1
    assert not _errors(caplog)


async def test_initial_off_retry_interval(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    freezer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dauerhafter Fehler: Tick-Retry höchstens alle 60 s (t=0, ≈60, ≈120)."""
    fake_switch.mode = "raise"
    entry = await setup_integration()
    assert fake_switch.off_calls() == 1
    await advance(hass, freezer, 125)
    assert fake_switch.off_calls() == 3
    assert controller_of(entry).initial_off_pending is True
    # error nur beim ersten Fehlversuch, danach debug
    assert caplog.text.count("Initial-OFF an switch.heater fehlgeschlagen") == 1


async def test_initial_off_single_attempt_on_sync_failure(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Review R4-2: HA läuft, Switch wirft sofort → genau ein turn_off beim Setup."""
    fake_switch.mode = "raise"
    with patch.object(migration, "async_check_legacy", wraps=migration.async_check_legacy) as check:
        await setup_integration()
        await hass.async_block_till_done()
    assert fake_switch.off_calls() == 1
    assert check.call_count == 1


async def test_initial_off_state_change_triggers_immediately(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch, freezer
) -> None:
    """State-Change auf on 10 s nach einem Fehlversuch → sofortiger Versuch."""
    fake_switch.mode = "raise"
    await setup_integration()
    assert fake_switch.off_calls() == 1
    await advance(hass, freezer, 10)
    hass.states.async_set(HEATER, "on")
    await hass.async_block_till_done()
    assert fake_switch.off_calls() == 2


async def test_initial_off_attribute_updates_do_not_retry(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch, freezer
) -> None:
    """Review R4-3: 10 Attribut-Updates in 30 s nach Fehlversuch → kein weiterer turn_off."""
    fake_switch.mode = "raise"
    await setup_integration()
    assert fake_switch.off_calls() == 1
    for rssi in range(10):
        await advance(hass, freezer, 3, step=3)
        hass.states.async_set(HEATER, "off", {"rssi": rssi})
        await hass.async_block_till_done()
    assert fake_switch.off_calls() == 1


async def test_observed_off_does_not_end_pending(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Ein bloß beobachtetes off beendet den ausstehenden Startup-OFF nicht."""
    fake_switch.mode = "raise"
    hass.states.async_set(HEATER, "on")
    entry = await setup_integration()
    controller = controller_of(entry)
    assert controller.initial_off_pending is True
    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    assert controller.initial_off_pending is True


async def test_start_ends_pending(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch, freezer
) -> None:
    """``start()`` beendet pending: kein Startup-OFF während running."""
    fake_switch.mode = "raise"
    set_sensor(hass, 40)
    entry = await setup_integration(stored=STORED)
    controller = controller_of(entry)
    assert controller.initial_off_pending is True
    fake_switch.mode = "normal"
    await controller.start()
    await hass.async_block_till_done()
    assert controller.initial_off_pending is False
    fake_switch.reset()
    await advance(hass, freezer, 120)
    assert controller.status == STATUS_RUNNING
    assert fake_switch.off_calls() == 0


async def test_stop_sends_off_even_if_state_off(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """``stop()`` bei State off → turn_off trotzdem."""
    entry = await setup_integration()
    fake_switch.reset()
    await controller_of(entry).stop()
    await hass.async_block_till_done()
    assert fake_switch.off_calls() == 1
