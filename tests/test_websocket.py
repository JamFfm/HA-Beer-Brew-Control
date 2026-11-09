"""WebSocket-Befehle (AK 21, AK 25; Design §8.2, §8.4)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import (
    DEFAULT_OPTIONS,
    HEATER,
    TWO_RESTS,
    FakeSwitch,
    controller_of,
    set_sensor,
    ws_command,
)
from homeassistant.core import HomeAssistant

from custom_components.brausteuerung.const import (
    MAX_RESTS,
    STATUS_IDLE,
    STATUS_RUNNING,
)
from custom_components.brausteuerung.controller import BrauController

STORED = {
    "recipe": TWO_RESTS,
    "recipe_name": "Helles",
    "library": [{"name": "Dunkel", "steps": TWO_RESTS[:1]}],
    "legacy_migrated": True,
}
REST = {"name": "Abmaischen", "temperature": 78.0, "duration": 5}


@pytest.fixture
async def controller(hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch):
    """Geladener Controller mit zwei Rasten und gültigem Sensor."""
    set_sensor(hass, 40)
    entry = await setup_integration(stored=STORED)
    return controller_of(entry)


@pytest.fixture
async def ws(hass: HomeAssistant, controller: BrauController, hass_ws_client):
    """WS-Client eines Admins."""
    return await hass_ws_client(hass)


@pytest.fixture
async def ws_user(
    hass: HomeAssistant, controller: BrauController, hass_ws_client, hass_read_only_access_token
):
    """WS-Client eines Nicht-Admins (read-only Benutzer)."""
    return await hass_ws_client(hass, hass_read_only_access_token)


@pytest.fixture
def ws_factory(hass: HomeAssistant, hass_ws_client):
    """Erzeugt einen Admin-Client erst nach eigenem Setup."""

    async def _create():
        return await hass_ws_client(hass)

    return _create


def _ok(response: dict[str, Any]) -> Any:
    assert response["success"], response
    return response["result"]


def _error(response: dict[str, Any]) -> str:
    assert not response["success"], response
    return response["error"]["code"]


# ---------------------------------------------------------------------------
# Abo
# ---------------------------------------------------------------------------


async def test_subscribe_sends_snapshot_and_updates(
    hass: HomeAssistant, controller: BrauController, ws
) -> None:
    """Ergebnis, dann sofort Snapshot; danach Event bei jeder Änderung; Unload → loaded False."""
    await ws.send_json_auto_id({"type": "brausteuerung/subscribe"})
    sub_id = (result := await ws.receive_json())["id"]
    assert _ok(result) is None
    event = await ws.receive_json()
    assert event["type"] == "event"
    assert event["id"] == sub_id
    assert event["event"]["loaded"] is True
    assert event["event"]["recipe"] == TWO_RESTS
    assert event["event"]["entities"]["setpoint"] == "sensor.brausteuerung_target_temperature"

    await controller.add_rest(**REST)
    event = await ws.receive_json()
    assert event["id"] == sub_id
    assert event["event"]["recipe"][-1] == REST

    assert await hass.config_entries.async_unload(controller._entry.entry_id)
    await hass.async_block_till_done()
    event = await ws.receive_json()
    assert event["event"] == {"loaded": False}


# ---------------------------------------------------------------------------
# Rezept
# ---------------------------------------------------------------------------


async def test_rest_commands(controller: BrauController, ws) -> None:
    """add/update/move/delete/clear ändern das Rezept, Ergebnis ``None``."""
    assert _ok(await ws_command(ws, "rest/add", **REST)) is None
    assert controller.recipe[-1].to_dict() == REST
    # Ohne Namen → Standardname.
    _ok(await ws_command(ws, "rest/add", temperature=72, duration=10))
    assert controller.recipe[-1].name == "Rast 4"

    _ok(await ws_command(ws, "rest/update", index=0, name="Neu", temperature=50, duration=20))
    assert controller.recipe[0].to_dict() == {"name": "Neu", "temperature": 50.0, "duration": 20}

    _ok(await ws_command(ws, "rest/move", index=0, direction=1))
    assert [r.name for r in controller.recipe][:2] == ["Verzuckerung", "Neu"]

    _ok(await ws_command(ws, "rest/delete", index=0))
    assert controller.recipe[0].name == "Neu"

    _ok(await ws_command(ws, "recipe/clear"))
    assert controller.recipe == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"temperature": -0.1, "duration": 10},
        {"temperature": 100.1, "duration": 10},
        {"temperature": "NaN", "duration": 10},
        {"temperature": True, "duration": 10},
        {"temperature": 60, "duration": 0},
        {"temperature": 60, "duration": 1.5},
        {"temperature": 60, "duration": -1},
        {"temperature": 60, "duration": True},
        {"temperature": 60, "duration": 10, "name": "a" * 65},
        {"temperature": 60, "duration": 10, "name": "a\x00b"},
        {"temperature": 60, "duration": 10, "name": "a\u202eb"},
        {"temperature": 60, "duration": 10, "name": 5},
        {"duration": 10},
    ],
)
async def test_rest_add_validation(
    controller: BrauController, ws, payload: dict[str, Any]
) -> None:
    """Ungültige Eingaben → ``invalid_format``, Rezept und Name unverändert (AK 21)."""
    before = (controller.recipe, controller.recipe_name)
    assert _error(await ws_command(ws, "rest/add", **payload)) == "invalid_format"
    assert _error(await ws_command(ws, "rest/update", index=0, **payload)) == "invalid_format"
    assert (controller.recipe, controller.recipe_name) == before


async def test_rest_name_boundaries(controller: BrauController, ws) -> None:
    """64 Zeichen und ZWJ-Emoji sind erlaubt."""
    _ok(await ws_command(ws, "rest/add", name="a" * 64, temperature=60, duration=10))
    _ok(await ws_command(ws, "rest/add", name="👨\u200d🍳", temperature=60, duration=10))
    assert controller.recipe[-2].name == "a" * 64
    assert controller.recipe[-1].name == "👨\u200d🍳"


async def test_too_many_rests(hass: HomeAssistant, setup_integration, fake_switch, ws_factory):
    """31. Rast → ``too_many_rests``, Rezept unverändert (AK 21)."""
    rests = [{"name": f"R{i}", "temperature": 50.0, "duration": 1} for i in range(MAX_RESTS)]
    entry = await setup_integration(
        stored={"recipe": rests, "recipe_name": "", "library": [], "legacy_migrated": True}
    )
    controller = controller_of(entry)
    ws = await ws_factory()
    before = controller.recipe
    assert _error(await ws_command(ws, "rest/add", **REST)) == "too_many_rests"
    assert controller.recipe == before


@pytest.mark.parametrize(
    ("command", "payload", "code"),
    [
        ("rest/update", {"index": 5, "temperature": 60, "duration": 10}, "invalid_index"),
        ("rest/delete", {"index": 2}, "invalid_index"),
        ("rest/delete", {"index": -1}, "invalid_format"),
        ("rest/delete", {"index": True}, "invalid_format"),
        ("rest/delete", {"index": 0.5}, "invalid_format"),
        ("rest/move", {"index": 0, "direction": -1}, "invalid_index"),
        ("rest/move", {"index": 0, "direction": True}, "invalid_format"),
        ("rest/move", {"index": 0, "direction": 2}, "invalid_format"),
    ],
)
async def test_index_errors(
    controller: BrauController, ws, command: str, payload: dict[str, Any], code: str
) -> None:
    """Ungültige Positionen und Richtungen; Rezept unverändert."""
    before = controller.recipe
    assert _error(await ws_command(ws, command, **payload)) == code
    assert controller.recipe == before


async def test_recipe_locked_while_running(
    hass: HomeAssistant, controller: BrauController, ws
) -> None:
    """Bei running sind Rezeptänderungen gesperrt, Speichern in die Bibliothek nicht."""
    _ok(await ws_command(ws, "control/start"))
    assert controller.status == STATUS_RUNNING
    before = controller.recipe
    for command, payload in (
        ("rest/add", REST),
        ("rest/update", {"index": 0, **REST}),
        ("rest/delete", {"index": 0}),
        ("rest/move", {"index": 0, "direction": 1}),
        ("recipe/clear", {}),
        ("library/load", {"name": "Dunkel"}),
    ):
        assert _error(await ws_command(ws, command, **payload)) == "recipe_locked", command
    assert controller.recipe == before
    _ok(await ws_command(ws, "library/save", name="Helles"))


# ---------------------------------------------------------------------------
# Bibliothek
# ---------------------------------------------------------------------------


async def test_library_commands(controller: BrauController, ws) -> None:
    """save (mit Überschreib-Flag), load, delete und Fehlerfälle."""
    _ok(await ws_command(ws, "library/save", name="  Helles  "))
    assert [e.name for e in controller.library] == ["Dunkel", "Helles"]
    assert controller.recipe_name == "Helles"

    assert _error(await ws_command(ws, "library/save", name="Dunkel")) == "name_exists"
    _ok(await ws_command(ws, "library/save", name="Dunkel", overwrite=True))
    assert [e.name for e in controller.library] == ["Dunkel", "Helles"]
    assert len(controller.library[0].steps) == 2

    _ok(await ws_command(ws, "library/load", name="Helles"))
    assert controller.recipe_name == "Helles"
    _ok(await ws_command(ws, "library/delete", name="Helles"))
    assert [e.name for e in controller.library] == ["Dunkel"]

    assert _error(await ws_command(ws, "library/load", name="Gibt es nicht")) == (
        "recipe_not_found"
    )
    assert _error(await ws_command(ws, "library/delete", name="Gibt es nicht")) == (
        "recipe_not_found"
    )
    for payload in ({"name": ""}, {"name": "a" * 65}, {"name": 5}, {"name": "x", "overwrite": 1}):
        assert _error(await ws_command(ws, "library/save", **payload)) == "invalid_format"
    assert _error(await ws_command(ws, "library/load", name=5)) == "invalid_format"


async def test_library_import_admin(controller: BrauController, ws) -> None:
    """Admin: Ergebnis ``{imported, skipped}``; Liste > 500 → ``invalid_format``."""
    result = _ok(
        await ws_command(
            ws,
            "library/import",
            recipes=[{"name": "Weizen", "steps": TWO_RESTS}, {"name": "Dunkel", "steps": []}],
            active_recipe_name="Weizen",
        )
    )
    assert result == {"imported": 1, "skipped": 1}
    too_many = [{"name": f"R{i}", "steps": []} for i in range(501)]
    assert _error(await ws_command(ws, "library/import", recipes=too_many)) == "invalid_format"
    assert _error(await ws_command(ws, "library/import", recipes="x")) == "invalid_format"


# ---------------------------------------------------------------------------
# Ablauf
# ---------------------------------------------------------------------------


async def test_control_commands(
    hass: HomeAssistant, controller: BrauController, ws, fake_switch: FakeSwitch
) -> None:
    """start → next_rest → stop; Vorbedingungen als Fehlercode."""
    assert _error(await ws_command(ws, "control/next_rest")) == "not_running"
    _ok(await ws_command(ws, "control/start"))
    assert controller.status == STATUS_RUNNING
    assert _error(await ws_command(ws, "control/start")) == "already_running"
    _ok(await ws_command(ws, "control/next_rest"))
    assert controller.step_index == 1
    fake_switch.reset()
    _ok(await ws_command(ws, "control/stop"))
    await hass.async_block_till_done()
    assert controller.status == STATUS_IDLE
    assert fake_switch.off_calls(HEATER) == 1


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------


async def test_settings_update(hass: HomeAssistant, controller: BrauController, ws) -> None:
    """Admin ändert Einstellungen; Validierung und Sperre bei running."""
    _ok(await ws_command(ws, "settings/update", hysteresis=0.5, safety_offset=12))
    assert controller.hysteresis == 0.5
    assert controller.safety_offset == 12.0
    assert controller._entry.options["hysteresis"] == 0.5

    for payload in (
        {},
        {"hysteresis": 0.05},
        {"hysteresis": 5.1},
        {"hysteresis": True},
        {"safety_offset": 20.5},
        {"sensor_entity": "light.x"},
        {"heater_entity": "sensor.x"},
        {"language": "en"},
    ):
        assert _error(await ws_command(ws, "settings/update", **payload)) == "invalid_format"
    assert controller.hysteresis == 0.5

    _ok(await ws_command(ws, "control/start"))
    assert (
        _error(await ws_command(ws, "settings/update", heater_entity="switch.other"))
        == "settings_locked"
    )
    assert controller.heater_entity == HEATER
    # Regelparameter bleiben bei running änderbar.
    _ok(await ws_command(ws, "settings/update", hysteresis=1.5))
    assert controller.hysteresis == 1.5


async def test_display_update(controller: BrauController, ws_user) -> None:
    """Sprache/Einheit für alle Benutzer; mindestens ein Feld."""
    _ok(await ws_command(ws_user, "display/update", language="en"))
    assert controller.language == "en"
    _ok(await ws_command(ws_user, "display/update", unit="°F"))
    assert controller.unit == "°F"
    for payload in ({}, {"language": "fr"}, {"unit": "K"}, {"hysteresis": 1}):
        assert _error(await ws_command(ws_user, "display/update", **payload)) == "invalid_format"


# ---------------------------------------------------------------------------
# Rechte (AK 25) und not_loaded
# ---------------------------------------------------------------------------


async def test_non_admin_permissions(
    hass: HomeAssistant, controller: BrauController, ws_user, fake_switch: FakeSwitch
) -> None:
    """Nicht-Admin: settings/update und library/import → ``unauthorized``, Rest erlaubt."""
    assert _error(await ws_command(ws_user, "settings/update", hysteresis=0.5)) == "unauthorized"
    assert _error(await ws_command(ws_user, "library/import", recipes=[])) == "unauthorized"
    assert controller.hysteresis == DEFAULT_OPTIONS["hysteresis"]

    _ok(await ws_command(ws_user, "rest/add", **REST))
    _ok(await ws_command(ws_user, "rest/update", index=0, **REST))
    _ok(await ws_command(ws_user, "rest/move", index=0, direction=1))
    _ok(await ws_command(ws_user, "rest/delete", index=0))
    _ok(await ws_command(ws_user, "library/save", name="Mein Rezept"))
    _ok(await ws_command(ws_user, "library/load", name="Dunkel"))
    _ok(await ws_command(ws_user, "library/delete", name="Mein Rezept"))
    _ok(await ws_command(ws_user, "control/start"))
    _ok(await ws_command(ws_user, "control/next_rest"))
    _ok(await ws_command(ws_user, "control/stop"))
    _ok(await ws_command(ws_user, "recipe/clear"))
    await ws_user.send_json_auto_id({"type": "brausteuerung/subscribe"})
    assert _ok(await ws_user.receive_json()) is None
    assert (await ws_user.receive_json())["event"]["loaded"] is True


@pytest.mark.parametrize(
    ("command", "payload"),
    [
        ("subscribe", {}),
        ("rest/add", REST),
        ("rest/update", {"index": 0, **REST}),
        ("rest/delete", {"index": 0}),
        ("rest/move", {"index": 0, "direction": 1}),
        ("recipe/clear", {}),
        ("library/save", {"name": "x"}),
        ("library/load", {"name": "x"}),
        ("library/delete", {"name": "x"}),
        ("library/import", {"recipes": []}),
        ("control/start", {}),
        ("control/stop", {}),
        ("control/next_rest", {}),
        ("settings/update", {"hysteresis": 1}),
        ("display/update", {"unit": "°C"}),
    ],
)
async def test_not_loaded(
    hass: HomeAssistant, controller: BrauController, ws, command: str, payload: dict[str, Any]
) -> None:
    """Ohne geladenen Entry → ``not_loaded`` für jeden Befehl."""
    assert await hass.config_entries.async_unload(controller._entry.entry_id)
    await hass.async_block_till_done()
    assert _error(await ws_command(ws, command, **payload)) == "not_loaded"
