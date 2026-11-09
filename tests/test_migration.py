"""Migration von v2.x und Altlasten (AK 26, 27; Design §10)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import TWO_RESTS, FakeSwitch, controller_of, set_sensor, ws_command
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.brausteuerung import migration
from custom_components.brausteuerung.const import (
    DOMAIN,
    LEGACY_HELPERS,
    MAX_LIBRARY_RECIPES,
    STATUS_IDLE,
    STATUS_RUNNING,
    STORAGE_KEY,
)
from custom_components.brausteuerung.controller import BrauError
from custom_components.brausteuerung.logic import Rest

COMPACT = json.dumps(
    [{"n": "Eiweißrast", "t": 55, "d": 15}, {"n": "Verzuckerung", "t": 65, "d": 30}]
)
LONG = json.dumps(TWO_RESTS)


def _set_helpers(hass: HomeAssistant, **overrides: str) -> None:
    values = {
        "input_text.brau_sensor_entity": "sensor.mash",
        "input_text.brau_heater_entity": "switch.heater",
        "input_number.brau_hysterese": "0.5",
        "input_number.brau_sicherheits_offset": "8.0",
        "input_select.brau_language": "de",
        "input_select.brau_unit": "°F",
        "input_text.brau_rezept_json": COMPACT,
        "input_text.brau_rezept_name": "Helles",
    }
    values.update(overrides)
    for entity_id, state in values.items():
        hass.states.async_set(entity_id, state)


def _issue(hass: HomeAssistant, issue_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


# ---------------------------------------------------------------------------
# read_legacy (§10.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe_json", [COMPACT, LONG])
async def test_read_legacy_valid(hass: HomeAssistant, recipe_json: str) -> None:
    """Gültige Helfer (kompakte und lange Form) werden übernommen."""
    _set_helpers(hass, **{"input_text.brau_rezept_json": recipe_json})
    values = migration.read_legacy(hass)
    assert values.sensor_entity == "sensor.mash"
    assert values.heater_entity == "switch.heater"
    assert values.hysteresis == 0.5
    assert values.safety_offset == 8.0
    assert values.language == "de"
    assert values.unit == "°F"
    assert values.recipe == (
        Rest("Eiweißrast", 55.0, 15),
        Rest("Verzuckerung", 65.0, 30),
    )
    assert values.recipe_name == "Helles"
    assert migration.build_legacy_import(values) == {
        "recipe": TWO_RESTS,
        "recipe_name": "Helles",
    }


async def test_read_legacy_invalid(hass: HomeAssistant) -> None:
    """Ungültige Werte → ``None`` (Defaults); „ “ als Name → leer."""
    _set_helpers(
        hass,
        **{
            "input_text.brau_sensor_entity": "light.x",
            "input_text.brau_heater_entity": "unknown",
            "input_number.brau_hysterese": "0",
            "input_number.brau_sicherheits_offset": "25",
            "input_select.brau_language": "fr",
            "input_select.brau_unit": "K",
            "input_text.brau_rezept_json": '[{"n": "x", "t": 200, "d": 10}]',
            "input_text.brau_rezept_name": " ",
        },
    )
    values = migration.read_legacy(hass)
    assert values == migration.LegacyValues(recipe_name="")
    assert migration.build_legacy_import(values) == {}


async def test_read_legacy_missing(hass: HomeAssistant) -> None:
    """Ohne Helfer ist alles ``None``."""
    assert migration.read_legacy(hass) == migration.LegacyValues()


# ---------------------------------------------------------------------------
# Übernahme beim Setup (AK 26)
# ---------------------------------------------------------------------------


async def test_legacy_import_once(
    hass: HomeAssistant, setup_integration, fake_switch, hass_storage: dict[str, Any]
) -> None:
    """Erste Übernahme aus ``legacy_import``; danach ``data={}``; kein zweites Migrieren."""
    payload = {"recipe": TWO_RESTS, "recipe_name": "Helles"}
    entry = await setup_integration(data={"legacy_import": payload})
    controller = controller_of(entry)
    assert controller.recipe_name == "Helles"
    assert len(controller.recipe) == 2
    assert entry.data == {}
    assert hass_storage[STORAGE_KEY]["data"]["legacy_migrated"] is True

    # Änderung und erneuter legacy_import: zweites Setup migriert nicht erneut.
    await controller.clear_recipe()
    hass.config_entries.async_update_entry(entry, data={"legacy_import": payload})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restarted = controller_of(entry)
    assert restarted.recipe == ()
    assert restarted.recipe_name == "Helles"
    assert entry.data == {}


async def test_legacy_import_invalid_gives_defaults(
    hass: HomeAssistant, setup_integration, fake_switch
) -> None:
    """Ungültige Legacy-Daten werden verworfen."""
    payload = {"recipe": [{"name": "x", "temperature": 500, "duration": 1}], "recipe_name": 5}
    entry = await setup_integration(data={"legacy_import": payload})
    controller = controller_of(entry)
    assert controller.recipe == ()
    assert controller.recipe_name == ""
    assert entry.data == {}


async def test_existing_store_is_not_overwritten(
    hass: HomeAssistant, setup_integration, fake_switch
) -> None:
    """Vorhandener Store (Neuhinzufügen) bleibt unangetastet."""
    stored = {"recipe": [], "recipe_name": "Bestand", "library": [], "legacy_migrated": True}
    entry = await setup_integration(
        stored=stored, data={"legacy_import": {"recipe": TWO_RESTS, "recipe_name": "Alt"}}
    )
    controller = controller_of(entry)
    assert controller.recipe_name == "Bestand"
    assert controller.recipe == ()
    assert entry.data == {}


# ---------------------------------------------------------------------------
# Bibliotheksübergabe (§10.2)
# ---------------------------------------------------------------------------


async def test_import_library(hass: HomeAssistant, setup_integration, fake_switch) -> None:
    """Validiert, überschreibt nicht, zählt übersprungene Einträge."""
    stored = {
        "recipe": [],
        "recipe_name": "",
        "library": [{"name": "Helles", "steps": TWO_RESTS}],
        "legacy_migrated": True,
    }
    entry = await setup_integration(stored=stored)
    controller = controller_of(entry)
    result = await controller.import_library(
        [
            {"name": "Helles", "steps": []},  # vorhanden → kein Überschreiben
            {"name": "Dunkel", "steps": TWO_RESTS},
            {"name": "Dunkel", "steps": []},  # doppelt im Import
            {"name": "", "steps": []},  # ungültig
            {"name": "Kaputt", "steps": [{"temperature": 999, "duration": 1}]},
            "kein dict",
        ],
        active_recipe_name="Dunkel",
    )
    assert result == {"imported": 1, "skipped": 5}
    assert [e.name for e in controller.library] == ["Helles", "Dunkel"]
    assert controller.library[0].steps != ()
    assert controller.recipe_name == "Dunkel"

    # Aktiver Name wird nur übernommen, wenn er leer ist.
    result = await controller.import_library([], active_recipe_name="Anderes")
    assert result == {"imported": 0, "skipped": 0}
    assert controller.recipe_name == "Dunkel"


async def test_import_library_respects_limit(
    hass: HomeAssistant, setup_integration, fake_switch
) -> None:
    """Einträge über ``MAX_LIBRARY_RECIPES`` zählen als übersprungen."""
    library = [{"name": f"R{i}", "steps": []} for i in range(MAX_LIBRARY_RECIPES - 1)]
    entry = await setup_integration(
        stored={"recipe": [], "recipe_name": "", "library": library, "legacy_migrated": True}
    )
    controller = controller_of(entry)
    result = await controller.import_library(
        [{"name": "A", "steps": []}, {"name": "B", "steps": []}]
    )
    assert result == {"imported": 1, "skipped": 1}
    assert len(controller.library) == MAX_LIBRARY_RECIPES


# ---------------------------------------------------------------------------
# Altlasten: Repair-Issues und Start-Sperre (AK 27)
# ---------------------------------------------------------------------------


async def test_repair_issues_created_and_deleted(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Alte Automation und Helfer → Repair-Issues; nach dem Entfernen gelöscht."""
    hass.states.async_set("automation.brausteuerung_raststufe", "off")
    hass.states.async_set("automation.alte_notaus", "off", {"id": "brausteuerung_notaus"})
    hass.states.async_set(LEGACY_HELPERS[0], "[]")
    set_sensor(hass, 40)
    entry = await setup_integration(
        stored={"recipe": TWO_RESTS, "recipe_name": "", "library": [], "legacy_migrated": True}
    )
    controller = controller_of(entry)

    issue = _issue(hass, "legacy_automations")
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.is_fixable is False
    assert issue.translation_placeholders == {
        "entities": "automation.alte_notaus, automation.brausteuerung_raststufe"
    }
    helpers = _issue(hass, "legacy_helpers")
    assert helpers is not None
    assert helpers.severity is ir.IssueSeverity.WARNING

    # Ausgeschaltete Altautomationen sperren den Start nicht; start prüft erneut.
    hass.states.async_remove("automation.brausteuerung_raststufe")
    hass.states.async_remove("automation.alte_notaus")
    hass.states.async_remove(LEGACY_HELPERS[0])
    await controller.start()
    assert controller.status == STATUS_RUNNING
    assert _issue(hass, "legacy_automations") is None
    assert _issue(hass, "legacy_helpers") is None


async def test_start_blocked_by_active_legacy_automation(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Eine eingeschaltete Legacy-Automation sperrt den Start."""
    hass.states.async_set("automation.brausteuerung_raststufe", "on")
    set_sensor(hass, 40)
    entry = await setup_integration(
        stored={"recipe": TWO_RESTS, "recipe_name": "", "library": [], "legacy_migrated": True}
    )
    controller = controller_of(entry)
    fake_switch.reset()
    with pytest.raises(BrauError) as err:
        await controller.start()
    assert err.value.code == "legacy_automations_active"
    assert controller.status == STATUS_IDLE
    assert fake_switch.on_calls() == 0


async def test_own_brausteuerung_automation_does_not_block_start(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> None:
    """Eigene automation.brausteuerung_* (UI-ID numerisch) ist keine Altlast."""
    hass.states.async_set(
        "automation.brausteuerung_start_am_morgen", "on", {"id": "1730900000000"}
    )
    set_sensor(hass, 40)
    entry = await setup_integration(
        stored={"recipe": TWO_RESTS, "recipe_name": "", "library": [], "legacy_migrated": True}
    )
    controller = controller_of(entry)
    await controller.start()
    assert controller.status == STATUS_RUNNING
    assert _issue(hass, "legacy_automations") is None


class _FakeResources:
    """Minimaler Ersatz der Lovelace-Ressourcensammlung."""

    def __init__(self, urls: list[str], fail: bool = False) -> None:
        self.urls = urls
        self.fail = fail

    async def async_get_info(self) -> dict[str, int]:
        if self.fail:
            raise RuntimeError("interne API geändert")
        return {"resources": len(self.urls)}

    def async_items(self) -> list[dict[str, str]]:
        return [{"url": url, "res_type": "module"} for url in self.urls]


class _FakeLovelace:
    def __init__(self, resources: _FakeResources) -> None:
        self.resources = resources


@pytest.mark.parametrize(
    ("urls", "fail", "expected"),
    [
        (["/local/brausteuerung-card.js?v=2.6.6"], False, True),
        (["/local/andere-karte.js"], False, False),
        (["/local/brausteuerung-card.js"], True, False),
    ],
)
async def test_legacy_resource_issue(
    hass: HomeAssistant,
    setup_integration,
    fake_switch,
    urls: list[str],
    fail: bool,
    expected: bool,
) -> None:
    """Alte Lovelace-Ressource → Repair-Issue (nur im async_at_started-Durchlauf)."""
    hass.data["lovelace"] = _FakeLovelace(_FakeResources(urls, fail))
    await setup_integration()
    assert (_issue(hass, "legacy_resource") is not None) is expected


_IMPORT_PAYLOAD = {
    "recipes": [{"name": "Dunkel", "steps": TWO_RESTS}, {"name": "", "steps": []}],
    "active_recipe_name": "Dunkel",
}


async def test_library_import_ws_admin(
    hass: HomeAssistant, setup_integration, fake_switch, hass_ws_client
) -> None:
    """``library/import`` als Admin: validiert, Ergebnis ``{imported, skipped}`` (AK 26)."""
    entry = await setup_integration(
        stored={"recipe": [], "recipe_name": "", "library": [], "legacy_migrated": True}
    )
    client = await hass_ws_client(hass)
    response = await ws_command(client, "library/import", **_IMPORT_PAYLOAD)
    assert response["success"], response
    assert response["result"] == {"imported": 1, "skipped": 1}
    controller = controller_of(entry)
    assert [e.name for e in controller.library] == ["Dunkel"]
    assert controller.recipe_name == "Dunkel"


async def test_library_import_ws_read_only_user(
    hass: HomeAssistant,
    setup_integration,
    fake_switch,
    hass_ws_client,
    hass_read_only_access_token,
) -> None:
    """``library/import`` als read-only Benutzer → ``unauthorized``, nichts übernommen (AK 25)."""
    entry = await setup_integration(
        stored={"recipe": [], "recipe_name": "", "library": [], "legacy_migrated": True}
    )
    client = await hass_ws_client(hass, hass_read_only_access_token)
    response = await ws_command(client, "library/import", **_IMPORT_PAYLOAD)
    assert not response["success"]
    assert response["error"]["code"] == "unauthorized"
    controller = controller_of(entry)
    assert controller.library == ()
    assert controller.recipe_name == ""


async def test_legacy_resource_without_lovelace(hass: HomeAssistant) -> None:
    """Ohne Lovelace-Daten wird die Ressourcenprüfung übersprungen."""
    assert await migration.async_check_legacy(hass, check_resource=True) is False
    assert _issue(hass, "legacy_resource") is None
