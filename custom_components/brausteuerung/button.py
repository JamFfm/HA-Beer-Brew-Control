"""Buttons Start, Stopp und Nächste Rast (Design §8.5)."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BrauConfigEntry
from .controller import BrauController
from .entity import BrauEntity


@dataclass(frozen=True, kw_only=True)
class BrauButtonDescription(ButtonEntityDescription):
    """Beschreibung eines Buttons samt Controller-Operation."""

    press_fn: Callable[[BrauController], Coroutine[Any, Any, None]]


BUTTONS: tuple[BrauButtonDescription, ...] = (
    BrauButtonDescription(key="start", translation_key="start", press_fn=lambda c: c.start()),
    BrauButtonDescription(key="stop", translation_key="stop", press_fn=lambda c: c.stop()),
    BrauButtonDescription(
        key="next_rest", translation_key="next_rest", press_fn=lambda c: c.next_rest()
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BrauConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Legt die Buttons an."""
    controller = entry.runtime_data
    async_add_entities(BrauButton(controller, entry, description) for description in BUTTONS)


class BrauButton(BrauEntity, ButtonEntity):
    """Ablauf-Button; Fehler kommen als ``ServiceValidationError`` (``BrauError``)."""

    _entity_platform = Platform.BUTTON
    entity_description: BrauButtonDescription

    async def async_press(self) -> None:
        """Führt die Operation aus."""
        await self.entity_description.press_fn(self._controller)
