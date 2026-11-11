"""Config Flow und Options Flow der Brausteuerung (Design §8.1).

Validierung in zwei Stufen: Die Selektoren prüfen Domäne und Bereich (HA meldet
Verstöße als ``InvalidData`` am Feld), der Step-Code prüft danach mit den
``logic``-Validatoren alles, was die Selektoren durchlassen (z. B. Registry-UUIDs
oder ``NaN``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import logic, migration
from .const import (
    CONF_HEATER_ENTITY,
    CONF_HYSTERESIS,
    CONF_LANGUAGE,
    CONF_SAFETY_OFFSET,
    CONF_SENSOR_ENTITY,
    CONF_UNIT,
    DEFAULT_HYSTERESIS,
    DEFAULT_LANGUAGE,
    DEFAULT_SAFETY_OFFSET,
    DEFAULT_UNIT,
    DEVICE_NAME,
    DOMAIN,
    LANGUAGES,
    MAX_HYSTERESIS,
    MAX_SAFETY_OFFSET,
    MIN_HYSTERESIS_INPUT,
    MIN_SAFETY_OFFSET,
    STATUS_RUNNING,
    UNITS,
)
from .validation import vol

_DEFAULTS: dict[str, Any] = {
    CONF_HYSTERESIS: DEFAULT_HYSTERESIS,
    CONF_SAFETY_OFFSET: DEFAULT_SAFETY_OFFSET,
    CONF_LANGUAGE: DEFAULT_LANGUAGE,
    CONF_UNIT: DEFAULT_UNIT,
}

# Feld → (Validator, Fehlerschlüssel) für die zweite Stufe im Step-Code.
_FIELD_CHECKS: dict[str, tuple[Callable[[Any], Any], str]] = {
    CONF_SENSOR_ENTITY: (lambda v: logic.validate_entity_id(v, "sensor"), "invalid_sensor"),
    CONF_HEATER_ENTITY: (lambda v: logic.validate_entity_id(v, "switch"), "invalid_heater"),
    CONF_HYSTERESIS: (logic.validate_hysteresis, "invalid_hysteresis"),
    CONF_SAFETY_OFFSET: (logic.validate_offset, "invalid_offset"),
    CONF_LANGUAGE: (logic.validate_language, "invalid_language"),
    CONF_UNIT: (logic.validate_unit, "invalid_unit"),
}


def _schema() -> vol.Schema:
    """Formular beider Flows (Selektoren nach §8.1)."""
    return vol.Schema(
        {
            vol.Required(CONF_SENSOR_ENTITY): EntitySelector(
                EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(CONF_HEATER_ENTITY): EntitySelector(
                EntitySelectorConfig(domain="switch")
            ),
            vol.Required(CONF_HYSTERESIS): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_HYSTERESIS_INPUT,
                    max=MAX_HYSTERESIS,
                    step=0.1,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(CONF_SAFETY_OFFSET): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_SAFETY_OFFSET,
                    max=MAX_SAFETY_OFFSET,
                    step=0.5,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(CONF_LANGUAGE): SelectSelector(
                SelectSelectorConfig(
                    options=list(LANGUAGES),
                    translation_key=CONF_LANGUAGE,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(CONF_UNIT): SelectSelector(
                SelectSelectorConfig(options=list(UNITS), mode=SelectSelectorMode.DROPDOWN)
            ),
        }
    )


def _validate(user_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Zweite Stufe: ``logic``-Validatoren; Rückgabe (Einstellungen, Feldfehler)."""
    settings: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, (validator, error) in _FIELD_CHECKS.items():
        try:
            settings[key] = validator(user_input.get(key))
        except logic.LogicError:
            errors[key] = error
    return settings, errors


def _legacy_suggestions(values: migration.LegacyValues) -> dict[str, Any]:
    """Validierte Legacy-Werte als Vorbelegung, sonst Defaults."""
    suggested = dict(_DEFAULTS)
    for key in _FIELD_CHECKS:
        if (value := getattr(values, key)) is not None:
            suggested[key] = value
    return suggested


class BrauConfigFlow(ConfigFlow, domain=DOMAIN):
    """Einrichtung; Einzelinstanz über ``single_config_entry`` im Manifest (AK 9)."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Einziger Schritt: Sensor, Heizung, Regelparameter, Anzeige."""
        errors: dict[str, str] = {}
        legacy = migration.read_legacy(self.hass)
        if user_input is not None:
            settings, errors = _validate(user_input)
            if not errors:
                legacy_import = migration.build_legacy_import(legacy)
                return self.async_create_entry(
                    title=DEVICE_NAME,
                    data={"legacy_import": legacy_import} if legacy_import else {},
                    options=settings,
                )
        suggested = user_input if user_input is not None else _legacy_suggestions(legacy)
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_schema(), suggested),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options Flow (ohne Reload, Live-Übernahme über den Update-Listener)."""
        return BrauOptionsFlow()


class BrauOptionsFlow(OptionsFlow):
    """Einstellungen ändern; Sensor/Heizung sind bei running gesperrt."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Einziger Schritt mit denselben Feldern wie der Config Flow."""
        errors: dict[str, str] = {}
        if user_input is not None:
            settings, errors = _validate(user_input)
            if not errors and self._entity_change_locked(settings):
                errors["base"] = "settings_locked"
            if not errors:
                return self.async_create_entry(data=settings)
        suggested = (
            user_input if user_input is not None else {**_DEFAULTS, **self.config_entry.options}
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(_schema(), suggested),
            errors=errors,
        )

    def _entity_change_locked(self, settings: dict[str, Any]) -> bool:
        """Sensor- oder Heizungswechsel während eines laufenden Brauvorgangs?"""
        controller = getattr(self.config_entry, "runtime_data", None)
        if controller is None or not controller.loaded or controller.status != STATUS_RUNNING:
            return False
        return (
            settings[CONF_SENSOR_ENTITY] != controller.sensor_entity
            or settings[CONF_HEATER_ENTITY] != controller.heater_entity
        )
