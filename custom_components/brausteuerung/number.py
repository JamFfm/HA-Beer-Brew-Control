"""Nummern für Hysterese und Sicherheits-Offset (Design §8.5, Rechte §8.4)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BrauConfigEntry
from .const import (
    CONF_HYSTERESIS,
    CONF_SAFETY_OFFSET,
    MAX_HYSTERESIS,
    MAX_SAFETY_OFFSET,
    MIN_HYSTERESIS_INPUT,
    MIN_SAFETY_OFFSET,
)
from .controller import BrauController
from .entity import BrauEntity


@dataclass(frozen=True, kw_only=True)
class BrauNumberDescription(NumberEntityDescription):
    """Beschreibung einer Einstellungs-Nummer."""

    value_fn: Callable[[BrauController], float]


NUMBERS: tuple[BrauNumberDescription, ...] = (
    BrauNumberDescription(
        key=CONF_HYSTERESIS,
        translation_key=CONF_HYSTERESIS,
        native_min_value=MIN_HYSTERESIS_INPUT,
        native_max_value=MAX_HYSTERESIS,
        native_step=0.1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda c: c.hysteresis,
    ),
    BrauNumberDescription(
        key=CONF_SAFETY_OFFSET,
        translation_key=CONF_SAFETY_OFFSET,
        native_min_value=MIN_SAFETY_OFFSET,
        native_max_value=MAX_SAFETY_OFFSET,
        native_step=0.5,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda c: c.safety_offset,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BrauConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Legt die Nummern an."""
    controller = entry.runtime_data
    async_add_entities(BrauNumber(controller, entry, description) for description in NUMBERS)


class BrauNumber(BrauEntity, NumberEntity):
    """Sicherheitsrelevante Einstellung; Ändern nur für Admins oder ohne Benutzer."""

    _entity_platform = Platform.NUMBER
    entity_description: BrauNumberDescription

    @property
    def native_value(self) -> float:
        """Aktueller Wert."""
        return self.entity_description.value_fn(self._controller)

    @property
    def native_unit_of_measurement(self) -> str:
        """Anzeigeeinheit (Temperaturdifferenz)."""
        return self._controller.unit

    async def async_set_native_value(self, value: float) -> None:
        """Übernimmt den Wert; Nicht-Admins erhalten ``Unauthorized`` (§8.4)."""
        context = self._context
        if context is not None and context.user_id is not None:
            user = await self.hass.auth.async_get_user(context.user_id)
            if user is None or not user.is_admin:
                raise Unauthorized(context=context)
        await self._controller.async_update_settings(**{self.entity_description.key: value})
