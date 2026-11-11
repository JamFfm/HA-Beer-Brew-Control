"""Migration von v2.x und Erkennung von Altlasten (Design §10).

``read_legacy`` liest die alten Helfer (Vorbelegung im Config Flow, Rezept/Name
als ``entry.data["legacy_import"]``). ``async_check_legacy`` meldet alte
Automationen, Helfer und die alte Lovelace-Ressource als Repair-Issues.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    LEGACY_AUTOMATION_ENTITY_IDS,
    LEGACY_AUTOMATION_IDS,
    LEGACY_HELPERS,
)
from .logic import (
    LogicError,
    Rest,
    parse_legacy_recipe,
    parse_sensor_value,
    recipe_from_list,
    recipe_to_list,
    validate_entity_id,
    validate_hysteresis,
    validate_language,
    validate_offset,
    validate_recipe_name,
    validate_unit,
)

_LOGGER = logging.getLogger(__name__)

LEGACY_RESOURCE_PATH = "/local/brausteuerung-card.js"

ISSUE_AUTOMATIONS = "legacy_automations"
ISSUE_HELPERS = "legacy_helpers"
ISSUE_RESOURCE = "legacy_resource"


@dataclass(frozen=True, slots=True)
class LegacyValues:
    """Validierte Werte der alten Helfer; ``None`` = fehlt oder ungültig."""

    sensor_entity: str | None = None
    heater_entity: str | None = None
    hysteresis: float | None = None
    safety_offset: float | None = None
    language: str | None = None
    unit: str | None = None
    recipe: tuple[Rest, ...] | None = None
    recipe_name: str | None = None


def _state(hass: HomeAssistant, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    return state.state


def _read(hass: HomeAssistant, entity_id: str, key: str, validator: Any) -> Any:
    """Liest einen Helfer; ungültig/fehlend → ``None`` mit info-Log ohne Inhalt."""
    raw = _state(hass, entity_id)
    if raw is None:
        return None
    try:
        return validator(raw)
    except LogicError:
        _LOGGER.info("Legacy-Wert %s ungültig; Default wird verwendet", key)
        return None


def _number(validator: Any) -> Any:
    def _parse(raw: str) -> float:
        value = parse_sensor_value(raw)
        if value is None:
            raise LogicError("invalid")
        return validator(value)

    return _parse


def _recipe(raw: str) -> tuple[Rest, ...]:
    recipe = parse_legacy_recipe(raw)
    if recipe is None:
        raise LogicError("invalid")
    return recipe


def read_legacy(hass: HomeAssistant) -> LegacyValues:
    """Liest und validiert die Helfer der v2.x (Design §10.1)."""
    return LegacyValues(
        sensor_entity=_read(
            hass,
            "input_text.brau_sensor_entity",
            "sensor_entity",
            lambda v: validate_entity_id(v.strip(), "sensor"),
        ),
        heater_entity=_read(
            hass,
            "input_text.brau_heater_entity",
            "heater_entity",
            lambda v: validate_entity_id(v.strip(), "switch"),
        ),
        hysteresis=_read(
            hass, "input_number.brau_hysterese", "hysteresis", _number(validate_hysteresis)
        ),
        safety_offset=_read(
            hass,
            "input_number.brau_sicherheits_offset",
            "safety_offset",
            _number(validate_offset),
        ),
        language=_read(hass, "input_select.brau_language", "language", validate_language),
        unit=_read(hass, "input_select.brau_unit", "unit", validate_unit),
        recipe=_read(hass, "input_text.brau_rezept_json", "recipe", _recipe),
        # Die alte Karte schreibt " " für „kein Name“ → validate macht daraus "".
        recipe_name=_read(hass, "input_text.brau_rezept_name", "recipe_name", validate_recipe_name),
    )


def build_legacy_import(values: LegacyValues) -> dict[str, Any]:
    """Rezept und Name für ``entry.data["legacy_import"]`` (leer, wenn nichts da ist)."""
    payload: dict[str, Any] = {}
    if values.recipe:
        payload["recipe"] = recipe_to_list(values.recipe)
    if values.recipe_name:
        payload["recipe_name"] = values.recipe_name
    return payload


def parse_legacy_import(raw: Any) -> tuple[tuple[Rest, ...] | None, str | None]:
    """Validiert ``legacy_import`` erneut; ungültige Teile → ``None``."""
    if not isinstance(raw, dict):
        return None, None
    recipe: tuple[Rest, ...] | None = None
    name: str | None = None
    if "recipe" in raw:
        try:
            recipe = recipe_from_list(raw["recipe"])
        except LogicError:
            _LOGGER.info("Legacy-Rezept ungültig; wird nicht übernommen")
    if "recipe_name" in raw:
        try:
            name = validate_recipe_name(raw["recipe_name"])
        except LogicError:
            _LOGGER.info("Legacy-Rezeptname ungültig; wird nicht übernommen")
    return recipe, name


def _legacy_automations(hass: HomeAssistant) -> list[Any]:
    """Nur die 5 alten Automationen (``id``-Attribut oder exakte Entity-ID)."""
    return [
        state
        for state in hass.states.async_all("automation")
        if state.attributes.get("id") in LEGACY_AUTOMATION_IDS
        or state.entity_id in LEGACY_AUTOMATION_ENTITY_IDS
    ]


def _set_issue(
    hass: HomeAssistant,
    issue_id: str,
    present: bool,
    severity: ir.IssueSeverity,
    placeholders: dict[str, str] | None = None,
) -> None:
    if present:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=severity,
            translation_key=issue_id,
            translation_placeholders=placeholders,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


async def _async_has_legacy_resource(hass: HomeAssistant) -> bool | None:
    """Best effort: alte Ressource ``/local/brausteuerung-card.js`` vorhanden?

    ``None`` heißt „nicht prüfbar“ (lovelace fehlt oder interne API weicht ab).
    """
    resources = getattr(hass.data.get("lovelace"), "resources", None)
    if resources is None:
        return None
    try:
        await resources.async_get_info()
        items = resources.async_items()
        return any(LEGACY_RESOURCE_PATH in str(item.get("url", "")) for item in items)
    except Exception:  # noqa: BLE001 - interne API, nur best effort
        _LOGGER.debug("Lovelace-Ressourcen nicht prüfbar", exc_info=True)
        return None


async def async_check_legacy(hass: HomeAssistant, *, check_resource: bool = False) -> bool:
    """Prüft Altlasten und pflegt die Repair-Issues (Design §10.3).

    Rückgabe ``True``, wenn eine alte Automation eingeschaltet ist (Start-Sperre).
    Die Lovelace-Prüfung läuft nur mit ``check_resource=True`` (async_at_started).
    """
    automations = _legacy_automations(hass)
    _set_issue(
        hass,
        ISSUE_AUTOMATIONS,
        bool(automations),
        ir.IssueSeverity.ERROR,
        {"entities": ", ".join(sorted(state.entity_id for state in automations))},
    )
    helpers = [entity_id for entity_id in LEGACY_HELPERS if hass.states.get(entity_id)]
    _set_issue(hass, ISSUE_HELPERS, bool(helpers), ir.IssueSeverity.WARNING)
    if check_resource:
        found = await _async_has_legacy_resource(hass)
        if found is not None:
            _set_issue(hass, ISSUE_RESOURCE, found, ir.IssueSeverity.WARNING)
    return any(state.state == STATE_ON for state in automations)
