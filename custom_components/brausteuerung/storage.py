"""Persistente Daten der Brausteuerung (Design §6).

Rezept, Rezeptname, Bibliothek und Migrationsmerker liegen im Store
``brausteuerung.data``. Laufzeitzustand wird hier nie gespeichert (S2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION
from .controller import BrauError
from .logic import LibraryRecipe, LogicError, Rest, recipe_from_list, recipe_to_list
from .logic import validate_recipe_name as _validate_recipe_name

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StoredData:
    """Validierter Inhalt des Stores."""

    recipe: tuple[Rest, ...] = ()
    recipe_name: str = ""
    library: tuple[LibraryRecipe, ...] = ()
    legacy_migrated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisierbare Form (Design §4.1)."""
        return {
            "recipe": recipe_to_list(self.recipe),
            "recipe_name": self.recipe_name,
            "library": [entry.to_dict() for entry in self.library],
            "legacy_migrated": self.legacy_migrated,
        }


class BrauStore:
    """Kapselt ``Store`` samt Validierung beim Laden und ``read_only``-Modus."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, atomic_writes=True
        )
        # True, wenn die Datei nicht gelesen werden konnte: dann nie überschreiben.
        self.read_only = False

    async def async_load(self) -> StoredData:
        """Lädt und validiert die Daten; Fehler sind nie fatal."""
        try:
            raw = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - Lesefehler dürfen das Setup nicht stoppen
            _LOGGER.error(
                "Speicher %s nicht lesbar (%s); Defaults, Änderungen gesperrt",
                STORAGE_KEY,
                type(err).__name__,
            )
            self.read_only = True
            return StoredData()
        if raw is None:
            return StoredData()
        if not isinstance(raw, dict):
            _LOGGER.warning("Speicher %s hat ein ungültiges Format; Defaults", STORAGE_KEY)
            return StoredData()
        return _validate(raw)

    async def async_save(self, data: StoredData) -> None:
        """Speichert; ``BrauError("storage_error")`` nur bei unerwarteter Ausnahme."""
        if self.read_only:
            raise BrauError("storage_error")
        try:
            await self._store.async_save(data.to_dict())
        except Exception as err:
            _LOGGER.exception("Speichern von %s fehlgeschlagen", STORAGE_KEY)
            raise BrauError("storage_error") from err


def _validate(raw: dict[str, Any]) -> StoredData:
    """Validiert jeden Eintrag einzeln über ``logic``."""
    try:
        recipe = recipe_from_list(raw.get("recipe", []))
    except LogicError:
        _LOGGER.warning("Gespeichertes Rezept ungültig; das aktive Rezept wird geleert")
        recipe = ()
    try:
        recipe_name = _validate_recipe_name(raw.get("recipe_name", ""))
    except LogicError:
        _LOGGER.warning("Gespeicherter Rezeptname ungültig; wird geleert")
        recipe_name = ""
    raw_library = raw.get("library", [])
    if not isinstance(raw_library, list):
        raw_library = []
        _LOGGER.warning("Gespeicherte Bibliothek ungültig; wird geleert")
    library: list[LibraryRecipe] = []
    names: set[str] = set()
    dropped = 0
    for item in raw_library:
        try:
            entry = LibraryRecipe.from_dict(item)
        except LogicError:
            dropped += 1
            continue
        if entry.name in names:
            dropped += 1
            continue
        names.add(entry.name)
        library.append(entry)
    if dropped:
        _LOGGER.warning("%d ungültige Bibliothekseinträge verworfen", dropped)
    return StoredData(
        recipe=recipe,
        recipe_name=recipe_name,
        library=tuple(library),
        legacy_migrated=raw.get("legacy_migrated") is True,
    )
