"""Rezept- und Bibliotheksoperationen, Persistenz, Store-Fehler (AK 21–23, Design §6, §7.6)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from conftest import TWO_RESTS, FakeSwitch, controller_of, set_sensor
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from custom_components.brausteuerung.const import STATUS_RUNNING, STORAGE_KEY
from custom_components.brausteuerung.controller import BrauController, BrauError

LIB_HELLES = {"name": "Helles", "steps": TWO_RESTS}
LIB_DUNKEL = {"name": "Dunkel", "steps": [{"name": "Rast", "temperature": 62.0, "duration": 45}]}
STORED = {
    "recipe": TWO_RESTS,
    "recipe_name": "",
    "library": [LIB_HELLES, LIB_DUNKEL],
    "legacy_migrated": True,
}


def _patch_load(func: Callable[[Store], Any]) -> Any:
    """Ersetzt ``Store.async_load`` nur für den Store der Brausteuerung."""
    original = Store.async_load

    async def _load(self: Store) -> Any:
        if self.key == STORAGE_KEY:
            return await func(self)
        return await original(self)

    return patch.object(Store, "async_load", _load)


def _patch_save(func: Callable[[Store, Any], Any]) -> Any:
    """Ersetzt ``Store.async_save`` nur für den Store der Brausteuerung."""
    original = Store.async_save

    async def _save(self: Store, data: Any) -> None:
        if self.key == STORAGE_KEY:
            return await func(self, data)
        return await original(self, data)

    return patch.object(Store, "async_save", _save)


async def _controller(
    hass: HomeAssistant, setup_integration, stored: Any = STORED
) -> BrauController:
    entry = await setup_integration(stored=stored)
    return controller_of(entry)


def _names(controller: BrauController) -> list[str]:
    return [entry.name for entry in controller.library]


# ---------------------------------------------------------------------------
# Rastoperationen
# ---------------------------------------------------------------------------


async def test_rest_operations(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """Anlegen (Standardname), Bearbeiten, Verschieben, Löschen, Leeren."""
    controller = await _controller(hass, setup_integration, {**STORED, "recipe": []})
    await controller.add_rest(50, 10)
    await controller.add_rest(60.5, 20, name="  Maltose  ")
    assert [r.name for r in controller.recipe] == ["Rast 1", "Maltose"]
    await controller.update_rest(0, 52, 15, name="")
    assert controller.recipe[0].temperature == 52.0
    assert controller.recipe[0].name == "Rast 1"
    await controller.move_rest(1, -1)
    assert [r.name for r in controller.recipe] == ["Maltose", "Rast 1"]
    await controller.delete_rest(0)
    assert [r.name for r in controller.recipe] == ["Rast 1"]
    await controller.clear_recipe()
    assert controller.recipe == ()


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ((-0.1, 10), "invalid_temperature"),
        ((100.1, 10), "invalid_temperature"),
        ((float("nan"), 10), "invalid_temperature"),
        ((True, 10), "invalid_temperature"),
        ((50, 0), "invalid_duration"),
        ((50, 1.5), "invalid_duration"),
        ((50, -1), "invalid_duration"),
        ((50, 10, "x" * 65), "invalid_name"),
        ((50, 10, "a\nb"), "invalid_name"),
    ],
)
async def test_invalid_rest_leaves_recipe_unchanged(
    hass: HomeAssistant, setup_integration, fake_switch, args: tuple[Any, ...], code: str
) -> None:
    """Ungültige Werte → Fehlercode, Rezept unverändert."""
    controller = await _controller(hass, setup_integration)
    before = controller.recipe
    with pytest.raises(BrauError) as err:
        await controller.add_rest(*args)
    assert err.value.code == code
    assert controller.recipe == before


async def test_too_many_rests(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """Die 31. Rast wird abgelehnt."""
    recipe = [{"name": f"R{i}", "temperature": 50.0, "duration": 1} for i in range(30)]
    controller = await _controller(hass, setup_integration, {**STORED, "recipe": recipe})
    with pytest.raises(BrauError) as err:
        await controller.add_rest(50, 10)
    assert err.value.code == "too_many_rests"
    assert len(controller.recipe) == 30


async def test_recipe_locked_while_running(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Edit/Delete/Clear/Move/Load während running → ``recipe_locked``; Speichern erlaubt."""
    set_sensor(hass, 40)
    controller = await _controller(hass, setup_integration)
    await controller.start()
    assert controller.status == STATUS_RUNNING
    before = controller.recipe
    for operation in (
        lambda: controller.add_rest(50, 10),
        lambda: controller.update_rest(0, 50, 10),
        lambda: controller.delete_rest(0),
        lambda: controller.move_rest(0, 1),
        controller.clear_recipe,
        lambda: controller.load_recipe("Dunkel"),
    ):
        with pytest.raises(BrauError) as err:
            await operation()
        assert err.value.code == "recipe_locked"
    assert controller.recipe == before
    await controller.save_recipe("Während")
    assert "Während" in _names(controller)


async def test_concurrent_add_rest(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """Zwei parallele ``add_rest`` → beide Rasten vorhanden (Daten-Lock)."""
    controller = await _controller(hass, setup_integration, {**STORED, "recipe": []})
    await asyncio.gather(controller.add_rest(50, 10, "A"), controller.add_rest(60, 10, "B"))
    assert sorted(r.name for r in controller.recipe) == ["A", "B"]


# ---------------------------------------------------------------------------
# Bibliothek (AK 23)
# ---------------------------------------------------------------------------


async def test_save_new_and_overwrite(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """Neuer Name → +1; vorhandener ohne Flag → Fehler; mit Flag → gleiche Position."""
    controller = await _controller(hass, setup_integration)
    await controller.save_recipe("  Weizen ")
    assert _names(controller) == ["Helles", "Dunkel", "Weizen"]
    assert controller.recipe_name == "Weizen"

    await controller.clear_recipe()
    await controller.add_rest(70, 5)
    with pytest.raises(BrauError) as err:
        await controller.save_recipe("Helles")
    assert err.value.code == "name_exists"
    assert controller.library[0].steps != controller.recipe

    await controller.save_recipe("Helles", overwrite=True)
    assert _names(controller) == ["Helles", "Dunkel", "Weizen"]
    assert controller.library[0].steps == controller.recipe
    assert controller.recipe_name == "Helles"


async def test_save_requires_name(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """Leerer Name → ``invalid_name``."""
    controller = await _controller(hass, setup_integration)
    with pytest.raises(BrauError) as err:
        await controller.save_recipe("   ")
    assert err.value.code == "invalid_name"


async def test_load_and_delete(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """Laden setzt Rezept und Namen; Löschen entfernt genau den Eintrag."""
    controller = await _controller(hass, setup_integration)
    await controller.load_recipe("Dunkel")
    assert controller.recipe_name == "Dunkel"
    assert controller.recipe == controller.library[1].steps
    await controller.delete_recipe("Helles")
    assert _names(controller) == ["Dunkel"]
    for operation in (controller.delete_recipe, controller.load_recipe):
        with pytest.raises(BrauError) as err:
            await operation("Helles")
        assert err.value.code == "recipe_not_found"


async def test_library_full(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """Bei 100 Einträgen → ``library_full``; Überschreiben bleibt möglich."""
    library = [{"name": f"Rezept {i}", "steps": TWO_RESTS} for i in range(100)]
    controller = await _controller(hass, setup_integration, {**STORED, "library": library})
    with pytest.raises(BrauError) as err:
        await controller.save_recipe("Nummer 101")
    assert err.value.code == "library_full"
    assert len(controller.library) == 100
    await controller.save_recipe("Rezept 5", overwrite=True)
    assert len(controller.library) == 100


async def test_persistence_after_restart(
    hass: HomeAssistant, setup_integration, fake_switch, hass_storage: dict[str, Any]
) -> None:
    """AK 22/23: Rezept > 255 Zeichen, Name und Bibliothek überdauern einen Neustart."""
    entry = await setup_integration(stored={**STORED, "recipe": []})
    controller = controller_of(entry)
    for i in range(8):
        await controller.add_rest(40 + i, 10 + i, name=f"Sehr lange Rastbezeichnung Nummer {i:02d}")
    await controller.save_recipe("Langes Rezept")
    long_recipe = controller.recipe
    assert len(str([r.to_dict() for r in long_recipe])) > 255
    assert hass_storage[STORAGE_KEY]["data"]["recipe_name"] == "Langes Rezept"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restarted = controller_of(entry)
    assert restarted.recipe == long_recipe
    assert restarted.recipe_name == "Langes Rezept"
    assert _names(restarted) == ["Helles", "Dunkel", "Langes Rezept"]


# ---------------------------------------------------------------------------
# Store-Fehler (Design §6, §15)
# ---------------------------------------------------------------------------


async def test_save_error_keeps_state(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """Unerwartete Ausnahme beim Speichern → ``storage_error``, Rezept unverändert."""
    controller = await _controller(hass, setup_integration)
    before = controller.recipe

    async def save_fail(_store: Store, _data: Any) -> None:
        raise OSError("Platte voll")

    with (
        _patch_save(save_fail),
        pytest.raises(BrauError) as err,
    ):
        await controller.add_rest(50, 10)
    assert err.value.code == "storage_error"
    assert controller.recipe == before


async def test_load_none_gives_defaults(
    hass: HomeAssistant, setup_integration, fake_switch
) -> None:
    """Kein Store vorhanden → Defaults, Änderungen möglich."""
    controller = await _controller(hass, setup_integration, stored=None)
    assert controller.recipe == ()
    assert controller.recipe_name == ""
    assert controller.library == ()
    await controller.add_rest(50, 10)
    assert len(controller.recipe) == 1


async def test_load_list_gives_defaults(
    hass: HomeAssistant,
    setup_integration,
    fake_switch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Store-Inhalt ist eine Liste statt dict → Defaults + warning."""
    controller = await _controller(hass, setup_integration, stored=[1, 2, 3])
    assert controller.recipe == ()
    assert controller.library == ()
    assert any(
        r.levelno == logging.WARNING and "ungültiges Format" in r.getMessage()
        for r in caplog.records
    )


async def test_load_invalid_entries_are_dropped(
    hass: HomeAssistant, setup_integration, fake_switch
) -> None:
    """Ungültige Rasten → aktives Rezept leer; ungültige Bibliothekseinträge einzeln weg."""
    stored = {
        "recipe": [{"name": "x", "temperature": 500, "duration": 10}],
        "recipe_name": "a\u202eb",
        "library": [LIB_HELLES, {"name": "", "steps": []}, LIB_HELLES, "kaputt"],
        "legacy_migrated": True,
    }
    controller = await _controller(hass, setup_integration, stored)
    assert controller.recipe == ()
    assert controller.recipe_name == ""
    assert _names(controller) == ["Helles"]


@pytest.mark.parametrize("error", [OSError("lesen"), HomeAssistantError("Wurzel kein Objekt")])
async def test_load_error_sets_read_only(
    hass: HomeAssistant,
    setup_integration,
    fake_switch: FakeSwitch,
    hass_storage: dict[str, Any],
    error: Exception,
) -> None:
    """Lese-Ausnahme → Defaults, ``read_only``: Mutation → ``storage_error``, keine Migration."""

    async def load_fail(_store: Store) -> Any:
        raise error

    with _patch_load(load_fail):
        entry = await setup_integration(data={"legacy_import": {"recipe_name": "Alt"}})
    controller = controller_of(entry)
    assert controller.recipe == ()
    # Regelung und Initial-OFF laufen normal.
    assert fake_switch.off_calls() == 1
    with pytest.raises(BrauError) as err:
        await controller.add_rest(50, 10)
    assert err.value.code == "storage_error"
    with pytest.raises(BrauError) as err:
        await controller.save_recipe("Neu")
    assert err.value.code == "storage_error"
    # Nichts wurde geschrieben, legacy_import bleibt für den nächsten Versuch.
    assert STORAGE_KEY not in hass_storage
    assert entry.data == {"legacy_import": {"recipe_name": "Alt"}}
