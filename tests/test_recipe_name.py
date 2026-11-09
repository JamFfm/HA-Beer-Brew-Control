"""Namenslogik des aktiven Rezepts (AK 24, Design §5, §7.6)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from conftest import DEFAULT_OPTIONS, TWO_RESTS, controller_of
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.brausteuerung.const import CONF_LANGUAGE
from custom_components.brausteuerung.controller import BrauController


async def _loaded(
    hass: HomeAssistant,
    setup_integration,
    names: list[str],
    load: str | None = "Helles",
    language: str = "de",
) -> BrauController:
    stored = {
        "recipe": [],
        "recipe_name": "",
        "library": [{"name": name, "steps": TWO_RESTS} for name in names],
        "legacy_migrated": True,
    }
    entry = await setup_integration(
        stored=stored, options={**DEFAULT_OPTIONS, CONF_LANGUAGE: language}
    )
    controller = controller_of(entry)
    if load is not None:
        await controller.load_recipe(load)
    return controller


@pytest.mark.parametrize(("language", "expected"), [("de", "Helles Neu"), ("en", "Helles New")])
async def test_first_change_adds_suffix(
    hass: HomeAssistant, setup_integration, fake_switch, language: str, expected: str
) -> None:
    """„Helles“ geladen, erste Änderung → Suffix der Integrationssprache."""
    controller = await _loaded(hass, setup_integration, ["Helles"], language=language)
    assert controller.recipe_name == "Helles"
    await controller.add_rest(70, 10)
    assert controller.recipe_name == expected


async def test_collision_counts_up(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """Existiert „Helles Neu“, wird es „Helles Neu 2“."""
    controller = await _loaded(hass, setup_integration, ["Helles", "Helles Neu"])
    await controller.delete_rest(0)
    assert controller.recipe_name == "Helles Neu 2"


async def test_second_change_keeps_name(
    hass: HomeAssistant, setup_integration, fake_switch
) -> None:
    """Zweite Änderung benennt nicht erneut um."""
    controller = await _loaded(hass, setup_integration, ["Helles"])
    await controller.move_rest(0, 1)
    assert controller.recipe_name == "Helles Neu"
    await controller.update_rest(0, 66, 30, name="Geändert")
    assert controller.recipe_name == "Helles Neu"


async def test_empty_name_stays_empty(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """Leerer Name erhält kein Suffix."""
    controller = await _loaded(hass, setup_integration, ["Helles"], load=None)
    await controller.add_rest(50, 10)
    assert controller.recipe_name == ""
    assert controller.snapshot()["recipe_name"] == ""


async def test_identical_edit_changes_nothing(
    hass: HomeAssistant, setup_integration, fake_switch
) -> None:
    """Edit mit identischen Werten → kein Suffix, kein Speichern."""
    controller = await _loaded(hass, setup_integration, ["Helles"])
    rest = TWO_RESTS[0]
    with patch.object(Store, "async_save") as save:
        await controller.update_rest(0, rest["temperature"], rest["duration"], name=rest["name"])
        await controller.clear_recipe()
        await controller.clear_recipe()
    # Nur clear_recipe (echte Änderung) speichert genau einmal.
    assert save.call_count == 1
    assert controller.recipe_name == "Helles Neu"


async def test_identical_edit_without_save(
    hass: HomeAssistant, setup_integration, fake_switch
) -> None:
    """Identischer Edit allein: kein Speichern, Name unverändert."""
    controller = await _loaded(hass, setup_integration, ["Helles"])
    rest: dict[str, Any] = TWO_RESTS[1]
    with patch.object(Store, "async_save") as save:
        await controller.update_rest(1, rest["temperature"], rest["duration"], name=rest["name"])
    assert save.call_count == 0
    assert controller.recipe_name == "Helles"


async def test_language_switch_replaces_suffix(
    hass: HomeAssistant, setup_integration, fake_switch
) -> None:
    """„Helles New“ nach Sprachwechsel auf de → „Helles Neu“ (kein „New Neu“)."""
    controller = await _loaded(
        hass, setup_integration, ["Helles New"], load="Helles New", language="en"
    )
    await controller.async_update_settings(language="de")
    await controller.add_rest(70, 10)
    assert controller.recipe_name == "Helles Neu"


async def test_save_then_change_again(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """Nach dem Speichern unter neuem Namen gilt dieser als Bibliotheksrezept."""
    controller = await _loaded(hass, setup_integration, ["Helles"])
    await controller.add_rest(70, 10)
    await controller.save_recipe("Helles Neu")
    await controller.add_rest(75, 10)
    assert controller.recipe_name == "Helles Neu 2"
