"""Validierungsbausteine für Schemas (Design §2, §8.2).

HA ab 2026.9 nutzt ``probatio`` (voluptuous-kompatibel), ältere Versionen
``voluptuous``. Grenzen und Zeichenregeln prüfen ausschließlich die
``logic``-Validatoren; ``vol.Length``/``vol.Range`` werden bewusst nicht genutzt.
Die Feld-Schemas am Ende teilen sich WebSocket-Befehle und Services.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:
    import probatio as vol
except ImportError:  # pragma: no cover - nur HA 2026.6–2026.8
    import voluptuous as vol

from . import logic
from .const import (
    CONF_HEATER_ENTITY,
    CONF_HYSTERESIS,
    CONF_LANGUAGE,
    CONF_SAFETY_OFFSET,
    CONF_SENSOR_ENTITY,
    CONF_UNIT,
    DOMAIN,
    MIN_HYSTERESIS_INPUT,
)


def _at_least_one(*keys: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Validator: mindestens einer der Schlüssel muss vorhanden sein."""

    def _check(value: dict[str, Any]) -> dict[str, Any]:
        if not any(k in value for k in keys):
            raise vol.Invalid(f"mindestens eines von {', '.join(keys)} erforderlich")
        return value

    return _check


def _wrap(func: Callable[..., Any], *args: Any) -> Callable[[Any], Any]:
    """Macht aus einem ``logic``-Validator einen Schema-Validator (LogicError → Invalid)."""

    def _validator(value: Any) -> Any:
        try:
            return func(value, *args)
        except logic.LogicError as err:
            raise vol.Invalid(err.code) from err

    return _validator


def number(value: Any) -> int | float:
    """Zahl (int/float), ``bool`` wird abgelehnt."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise vol.Invalid("expected a number")
    return value


def integer(value: Any) -> int:
    """Ganze Zahl, ``bool`` und ``float`` werden abgelehnt."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise vol.Invalid("expected an integer")
    return value


temperature = _wrap(logic.validate_temperature)
duration = _wrap(logic.validate_duration)
hysteresis = _wrap(logic.validate_hysteresis)
offset = _wrap(logic.validate_offset)
language = _wrap(logic.validate_language)
unit = _wrap(logic.validate_unit)


def rest_name(value: Any) -> Any:
    """Rastname prüfen; der Standardname wird erst im Controller vergeben."""
    if value is None:
        return None
    try:
        logic.validate_rest_name(value, 1)
    except logic.LogicError as err:
        raise vol.Invalid(err.code) from err
    return value


def recipe_name(value: Any) -> str:
    """Nicht leerer Rezeptname (getrimmt)."""
    try:
        return logic.validate_recipe_name(value, allow_empty=False)
    except logic.LogicError as err:
        raise vol.Invalid(err.code) from err


def entity_id(domain: str) -> Callable[[Any], str]:
    """Entity-ID der gegebenen Domäne."""
    return _wrap(logic.validate_entity_id, domain)


# Obergrenze der Roheinträge bei library/import (Design §8.2).
MAX_IMPORT_ITEMS = 500


def index(value: Any) -> int:
    """Rastposition: ganze Zahl ≥ 0 (die Obergrenze prüft der Controller)."""
    value = integer(value)
    if value < 0:
        raise vol.Invalid("invalid_index")
    return value


def direction(value: Any) -> int:
    """Verschieberichtung −1 (hoch) oder +1 (runter); ``True`` zählt nicht als 1."""
    value = integer(value)
    if value not in (-1, 1):
        raise vol.Invalid("expected -1 or 1")
    return value


def hysteresis_input(value: Any) -> float:
    """Hysterese der Eingabeschicht: 0,1–5 (Design §4.4)."""
    result = hysteresis(value)
    if result < MIN_HYSTERESIS_INPUT:
        raise vol.Invalid("invalid_hysteresis")
    return result


def string(value: Any) -> str:
    """Zeichenkette ohne Umwandlung (Zahlen werden abgelehnt)."""
    if not isinstance(value, str):
        raise vol.Invalid("expected a string")
    return value


def optional_string(value: Any) -> str | None:
    """Zeichenkette oder ``None``."""
    return None if value is None else string(value)


def boolean(value: Any) -> bool:
    """Echter Wahrheitswert (keine Umwandlung aus Strings/Zahlen)."""
    if not isinstance(value, bool):
        raise vol.Invalid("expected a boolean")
    return value


def raw_list(value: Any) -> list[Any]:
    """Liste mit höchstens ``MAX_IMPORT_ITEMS`` Roheinträgen (Inhalt prüft der Controller)."""
    if not isinstance(value, list):
        raise vol.Invalid("expected a list")
    if len(value) > MAX_IMPORT_ITEMS:
        raise vol.Invalid(f"höchstens {MAX_IMPORT_ITEMS} Einträge")
    return value


# ---------------------------------------------------------------------------
# Felder der WS-Befehle und Services (Design §8.2, §8.3)
# ---------------------------------------------------------------------------

REST_FIELDS: dict[Any, Any] = {
    vol.Optional("name"): rest_name,
    vol.Required("temperature"): temperature,
    vol.Required("duration"): duration,
}
INDEX_FIELDS: dict[Any, Any] = {vol.Required("index"): index}
UPDATE_REST_FIELDS: dict[Any, Any] = {**INDEX_FIELDS, **REST_FIELDS}
MOVE_FIELDS: dict[Any, Any] = {**INDEX_FIELDS, vol.Required("direction"): direction}
SAVE_FIELDS: dict[Any, Any] = {
    vol.Required("name"): recipe_name,
    vol.Optional("overwrite", default=False): boolean,
}
NAME_FIELDS: dict[Any, Any] = {vol.Required("name"): string}
IMPORT_FIELDS: dict[Any, Any] = {
    vol.Required("recipes"): raw_list,
    vol.Optional("active_recipe_name"): optional_string,
}
SETTINGS_FIELDS: dict[Any, Any] = {
    vol.Optional(CONF_SENSOR_ENTITY): entity_id("sensor"),
    vol.Optional(CONF_HEATER_ENTITY): entity_id("switch"),
    vol.Optional(CONF_HYSTERESIS): hysteresis_input,
    vol.Optional(CONF_SAFETY_OFFSET): offset,
}
DISPLAY_FIELDS: dict[Any, Any] = {
    vol.Optional(CONF_LANGUAGE): language,
    vol.Optional(CONF_UNIT): unit,
}


def ws_schema(command: str, fields: dict[Any, Any] | None = None) -> dict[Any, Any]:
    """Mapping-Schema eines WS-Befehls ``brausteuerung/<command>``."""
    return {vol.Required("type"): f"{DOMAIN}/{command}", **(fields or {})}


def ws_schema_at_least_one(command: str, fields: dict[Any, Any]) -> Any:
    """WS-Schema, bei dem mindestens eines der (optionalen) Felder gesetzt sein muss."""
    return vol.All(
        vol.Schema(ws_schema(command, fields)),
        _at_least_one(*(str(key) for key in fields)),
    )


WS_SETTINGS_UPDATE = ws_schema_at_least_one("settings/update", SETTINGS_FIELDS)
WS_DISPLAY_UPDATE = ws_schema_at_least_one("display/update", DISPLAY_FIELDS)
