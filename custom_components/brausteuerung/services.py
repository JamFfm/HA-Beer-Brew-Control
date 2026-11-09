"""HA-Services für Automationen und Skripte (Design §8.3).

Gleiche Schemas und Controller-Methoden wie die WebSocket-Befehle. Alle Benutzer
sind zugelassen (A10); Konfiguration läuft nicht über Services.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall, callback

from . import validation as v
from .const import DOMAIN
from .controller import BrauController, BrauError
from .websocket import async_get_controller

type _Operation = Callable[[BrauController, dict[str, Any]], Awaitable[Any]]

# Service → (Felder, Operation)
_SERVICES: dict[str, tuple[dict[Any, Any], _Operation]] = {
    "start": ({}, lambda c, _d: c.start()),
    "stop": ({}, lambda c, _d: c.stop()),
    "next_rest": ({}, lambda c, _d: c.next_rest()),
    "add_rest": (
        v.REST_FIELDS,
        lambda c, d: c.add_rest(d["temperature"], d["duration"], d.get("name")),
    ),
    "update_rest": (
        v.UPDATE_REST_FIELDS,
        lambda c, d: c.update_rest(d["index"], d["temperature"], d["duration"], d.get("name")),
    ),
    "delete_rest": (v.INDEX_FIELDS, lambda c, d: c.delete_rest(d["index"])),
    "move_rest": (v.MOVE_FIELDS, lambda c, d: c.move_rest(d["index"], d["direction"])),
    "clear_recipe": ({}, lambda c, _d: c.clear_recipe()),
    "save_recipe": (v.SAVE_FIELDS, lambda c, d: c.save_recipe(d["name"], d["overwrite"])),
    "load_recipe": (v.NAME_FIELDS, lambda c, d: c.load_recipe(d["name"])),
    "delete_recipe": (v.NAME_FIELDS, lambda c, d: c.delete_recipe(d["name"])),
}


def _handler(operation: _Operation) -> Callable[[ServiceCall], Awaitable[None]]:
    """Service-Handler: Controller auflösen, Operation ausführen."""

    async def _async_handle(call: ServiceCall) -> None:
        controller = async_get_controller(call.hass)
        if controller is None:
            # BrauError ist ein ServiceValidationError mit translation_domain=DOMAIN.
            raise BrauError("not_loaded")
        await operation(controller, dict(call.data))

    return _async_handle


@callback
def async_register(hass: HomeAssistant) -> None:
    """Registriert alle Services (einmal pro HA-Prozess aus ``async_setup``)."""
    for name, (fields, operation) in _SERVICES.items():
        hass.services.async_register(
            DOMAIN, name, _handler(operation), schema=v.vol.Schema(fields)
        )
