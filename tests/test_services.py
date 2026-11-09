"""HA-Services und Admin-Prüfung der Nummern (Design §8.3, §8.4)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import HEATER, TWO_RESTS, FakeSwitch, controller_of, set_sensor
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.brausteuerung.const import DOMAIN, STATUS_IDLE, STATUS_RUNNING
from custom_components.brausteuerung.controller import BrauController
from custom_components.brausteuerung.validation import vol

STORED = {
    "recipe": TWO_RESTS,
    "recipe_name": "Helles",
    "library": [{"name": "Dunkel", "steps": TWO_RESTS[:1]}],
    "legacy_migrated": True,
}
SERVICES = (
    "start",
    "stop",
    "next_rest",
    "add_rest",
    "update_rest",
    "delete_rest",
    "move_rest",
    "clear_recipe",
    "save_recipe",
    "load_recipe",
    "delete_recipe",
)


@pytest.fixture
async def controller(
    hass: HomeAssistant, setup_integration, fake_switch: FakeSwitch
) -> BrauController:
    """Geladener Controller mit zwei Rasten und gültigem Sensor."""
    set_sensor(hass, 40)
    entry = await setup_integration(stored=STORED)
    return controller_of(entry)


async def _call(hass: HomeAssistant, service: str, **data: Any) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True)


async def test_services_registered(hass: HomeAssistant, controller: BrauController) -> None:
    """Genau die 11 Services aus §8.3."""
    assert set(hass.services.async_services_for_domain(DOMAIN)) == set(SERVICES)


async def test_recipe_services(hass: HomeAssistant, controller: BrauController) -> None:
    """Rezeptservices verhalten sich wie die WS-Befehle."""
    await _call(hass, "add_rest", name="Abmaischen", temperature=78, duration=5)
    assert controller.recipe[-1].name == "Abmaischen"
    await _call(hass, "update_rest", index=2, temperature=77.5, duration=10)
    assert controller.recipe[2].to_dict() == {
        "name": "Rast 3",
        "temperature": 77.5,
        "duration": 10,
    }
    await _call(hass, "move_rest", index=2, direction=-1)
    assert [r.name for r in controller.recipe] == ["Eiweißrast", "Rast 3", "Verzuckerung"]
    await _call(hass, "delete_rest", index=1)
    assert len(controller.recipe) == 2
    await _call(hass, "clear_recipe")
    assert controller.recipe == ()


async def test_library_services(hass: HomeAssistant, controller: BrauController) -> None:
    """save (inkl. ``name_exists``/overwrite), load, delete."""
    await _call(hass, "save_recipe", name="Helles")
    assert [e.name for e in controller.library] == ["Dunkel", "Helles"]
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "save_recipe", name="Dunkel")
    assert err.value.translation_key == "name_exists"
    await _call(hass, "save_recipe", name="Dunkel", overwrite=True)
    await _call(hass, "load_recipe", name="Helles")
    assert controller.recipe_name == "Helles"
    await _call(hass, "delete_recipe", name="Helles")
    assert [e.name for e in controller.library] == ["Dunkel"]
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "load_recipe", name="Helles")
    assert err.value.translation_key == "recipe_not_found"
    assert err.value.translation_domain == DOMAIN


async def test_control_services(
    hass: HomeAssistant, controller: BrauController, fake_switch: FakeSwitch
) -> None:
    """start → next_rest → stop; Vorbedingung als ServiceValidationError."""
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "next_rest")
    assert err.value.translation_key == "not_running"
    await _call(hass, "start")
    assert controller.status == STATUS_RUNNING
    await _call(hass, "next_rest")
    assert controller.step_index == 1
    fake_switch.reset()
    await _call(hass, "stop")
    await hass.async_block_till_done()
    assert controller.status == STATUS_IDLE
    assert fake_switch.off_calls(HEATER) == 1


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("add_rest", {"temperature": 100.1, "duration": 5}),
        ("add_rest", {"temperature": 60, "duration": 1.5}),
        ("add_rest", {"temperature": True, "duration": 5}),
        ("add_rest", {"temperature": 60, "duration": 5, "name": "a" * 65}),
        ("update_rest", {"index": -1, "temperature": 60, "duration": 5}),
        ("move_rest", {"index": 0, "direction": 0}),
        ("save_recipe", {"name": ""}),
        ("load_recipe", {}),
    ],
)
async def test_service_validation(
    hass: HomeAssistant, controller: BrauController, service: str, data: dict[str, Any]
) -> None:
    """Schemafehler wie bei den WS-Befehlen; Rezept unverändert."""
    before = (controller.recipe, controller.library)
    with pytest.raises(vol.Invalid):
        await _call(hass, service, **data)
    assert (controller.recipe, controller.library) == before


async def test_recipe_locked_while_running(hass: HomeAssistant, controller: BrauController) -> None:
    """Sperre bei running gilt auch für Services."""
    await _call(hass, "start")
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "add_rest", temperature=60, duration=5)
    assert err.value.translation_key == "recipe_locked"


@pytest.mark.parametrize("service", SERVICES)
async def test_not_loaded(hass: HomeAssistant, controller: BrauController, service: str) -> None:
    """Ohne geladenen Entry → ServiceValidationError ``not_loaded``."""
    data: dict[str, Any] = {
        "add_rest": {"temperature": 60, "duration": 5},
        "update_rest": {"index": 0, "temperature": 60, "duration": 5},
        "delete_rest": {"index": 0},
        "move_rest": {"index": 0, "direction": 1},
        "save_recipe": {"name": "x"},
        "load_recipe": {"name": "x"},
        "delete_recipe": {"name": "x"},
    }.get(service, {})
    assert await hass.config_entries.async_unload(controller._entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, service, **data)
    assert err.value.translation_key == "not_loaded"
    assert err.value.translation_domain == DOMAIN


# ---------------------------------------------------------------------------
# number.set_value: Admin-Prüfung über den Context (§8.4)
# ---------------------------------------------------------------------------


@pytest.fixture
async def non_admin_user(hass: HomeAssistant) -> MockUser:
    """Benutzer der Gruppe „Users“: Steuerrecht auf Entitäten, aber kein Admin."""
    group = await hass.auth.async_get_group(GROUP_ID_USER)
    return MockUser(name="Nutzer", groups=[group]).add_to_hass(hass)


def _number_id(hass: HomeAssistant, controller: BrauController, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "number", DOMAIN, f"{controller._entry.entry_id}_{key}"
    )
    assert entity_id is not None
    return entity_id


async def _set_number(
    hass: HomeAssistant, entity_id: str, value: float, context: Context | None
) -> None:
    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: entity_id, "value": value},
        blocking=True,
        context=context,
    )


@pytest.mark.parametrize("key", ["hysteresis", "safety_offset"])
async def test_number_non_admin_unauthorized(
    hass: HomeAssistant, controller: BrauController, non_admin_user: MockUser, key: str
) -> None:
    """Nicht-Admin mit Steuerrecht auf Entitäten → ``Unauthorized``, Wert unverändert."""
    user = non_admin_user
    assert not user.is_admin
    entity_id = _number_id(hass, controller, key)
    before = controller.options[key]
    with pytest.raises(Unauthorized):
        await _set_number(hass, entity_id, 2.0, Context(user_id=user.id))
    assert controller.options[key] == before


async def test_number_admin_and_system_allowed(
    hass: HomeAssistant, controller: BrauController, hass_admin_user
) -> None:
    """Admin und Aufruf ohne Benutzer (Automation) dürfen ändern."""
    entity_id = _number_id(hass, controller, "hysteresis")
    await _set_number(hass, entity_id, 0.5, Context(user_id=hass_admin_user.id))
    assert controller.hysteresis == 0.5
    await _set_number(hass, entity_id, 1.5, None)
    assert controller.hysteresis == 1.5


async def test_select_and_button_for_non_admin(
    hass: HomeAssistant, controller: BrauController, non_admin_user: MockUser
) -> None:
    """Sprache/Einheit und Stopp bleiben für Nicht-Admins erlaubt."""
    context = Context(user_id=non_admin_user.id)
    registry = er.async_get(hass)
    entry_id = controller._entry.entry_id
    language = registry.async_get_entity_id("select", DOMAIN, f"{entry_id}_language")
    stop = registry.async_get_entity_id("button", DOMAIN, f"{entry_id}_stop")
    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: language, "option": "en"},
        blocking=True,
        context=context,
    )
    assert controller.language == "en"
    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: stop}, blocking=True, context=context
    )
    assert controller.status == STATUS_IDLE
