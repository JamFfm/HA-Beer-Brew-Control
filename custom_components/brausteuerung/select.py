"""Auswahl für Sprache und Anzeigeeinheit (Design §8.5)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BrauConfigEntry
from .const import CONF_LANGUAGE, CONF_UNIT, LANGUAGES, UNITS
from .controller import BrauController
from .entity import BrauEntity


@dataclass(frozen=True, kw_only=True)
class BrauSelectDescription(SelectEntityDescription):
    """Beschreibung einer Auswahl."""

    value_fn: Callable[[BrauController], str]


SELECTS: tuple[BrauSelectDescription, ...] = (
    BrauSelectDescription(
        key=CONF_LANGUAGE,
        translation_key=CONF_LANGUAGE,
        options=list(LANGUAGES),
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda c: c.language,
    ),
    BrauSelectDescription(
        key=CONF_UNIT,
        translation_key=CONF_UNIT,
        options=list(UNITS),
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda c: c.unit,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BrauConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Legt die Auswahlen an."""
    controller = entry.runtime_data
    async_add_entities(BrauSelect(controller, entry, description) for description in SELECTS)


class BrauSelect(BrauEntity, SelectEntity):
    """Anzeigeeinstellung (für alle Benutzer änderbar)."""

    _entity_platform = Platform.SELECT
    entity_description: BrauSelectDescription

    @property
    def current_option(self) -> str:
        """Gewählte Option."""
        return self.entity_description.value_fn(self._controller)

    async def async_select_option(self, option: str) -> None:
        """Übernimmt die Auswahl."""
        await self._controller.async_update_settings(**{self.entity_description.key: option})
