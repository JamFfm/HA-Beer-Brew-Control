"""WebSocket-Befehle für die Karte (Design §8.2, Rechte §8.4).

Decorator-Reihenfolge ist fest: ``websocket_command`` → ``require_admin`` (nur
``library/import`` und ``settings/update``) → ``async_response``. Schemafehler
beantwortet HA selbst mit ``invalid_format``, fehlende Adminrechte mit
``unauthorized``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import validation as v
from .const import DOMAIN
from .controller import BrauController, BrauError

type _Operation = Callable[[BrauController, dict[str, Any]], Awaitable[Any]]


@callback
def async_get_controller(hass: HomeAssistant) -> BrauController | None:
    """Controller des geladenen Entries oder ``None``."""
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        controller = getattr(entry, "runtime_data", None)
        if isinstance(controller, BrauController) and controller.loaded:
            return controller
    return None


async def _async_run(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    operation: _Operation,
    *,
    with_result: bool = False,
) -> None:
    """Führt eine Controller-Operation aus und beantwortet den Befehl."""
    controller = async_get_controller(hass)
    if controller is None:
        connection.send_error(msg["id"], "not_loaded", "HA-Beer-Brew-Control ist nicht geladen")
        return
    try:
        result = await operation(controller, msg)
    except BrauError as err:
        connection.send_error(msg["id"], err.translation_key, str(err))
        return
    connection.send_result(msg["id"], result if with_result else None)


def _fields(msg: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Nur die gesetzten Nutzfelder des Befehls."""
    return {key: msg[key] for key in keys if key in msg}


# ---------------------------------------------------------------------------
# Abo
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(v.ws_schema("subscribe"))
@callback
def ws_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Snapshot-Abo: Ergebnis, sofort ein Snapshot-Event, danach bei jeder Änderung."""
    controller = async_get_controller(hass)
    if controller is None:
        connection.send_error(msg["id"], "not_loaded", "HA-Beer-Brew-Control ist nicht geladen")
        return

    @callback
    def forward() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], controller.snapshot()))

    connection.subscriptions[msg["id"]] = controller.async_add_listener(forward)
    connection.send_result(msg["id"])
    forward()


# ---------------------------------------------------------------------------
# Rezept
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(v.ws_schema("rest/add", v.REST_FIELDS))
@websocket_api.async_response
async def ws_rest_add(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Rast anhängen."""
    await _async_run(
        hass,
        connection,
        msg,
        lambda c, m: c.add_rest(m["temperature"], m["duration"], m.get("name")),
    )


@websocket_api.websocket_command(v.ws_schema("rest/update", v.UPDATE_REST_FIELDS))
@websocket_api.async_response
async def ws_rest_update(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Rast ersetzen."""
    await _async_run(
        hass,
        connection,
        msg,
        lambda c, m: c.update_rest(m["index"], m["temperature"], m["duration"], m.get("name")),
    )


@websocket_api.websocket_command(v.ws_schema("rest/delete", v.INDEX_FIELDS))
@websocket_api.async_response
async def ws_rest_delete(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Rast entfernen."""
    await _async_run(hass, connection, msg, lambda c, m: c.delete_rest(m["index"]))


@websocket_api.websocket_command(v.ws_schema("rest/move", v.MOVE_FIELDS))
@websocket_api.async_response
async def ws_rest_move(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Rast verschieben."""
    await _async_run(
        hass, connection, msg, lambda c, m: c.move_rest(m["index"], m["direction"])
    )


@websocket_api.websocket_command(v.ws_schema("recipe/clear"))
@websocket_api.async_response
async def ws_recipe_clear(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Rezept leeren."""
    await _async_run(hass, connection, msg, lambda c, _m: c.clear_recipe())


# ---------------------------------------------------------------------------
# Bibliothek
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(v.ws_schema("library/save", v.SAVE_FIELDS))
@websocket_api.async_response
async def ws_library_save(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Aktives Rezept speichern (Überschreiben nur mit ``overwrite``)."""
    await _async_run(
        hass, connection, msg, lambda c, m: c.save_recipe(m["name"], m["overwrite"])
    )


@websocket_api.websocket_command(v.ws_schema("library/load", v.NAME_FIELDS))
@websocket_api.async_response
async def ws_library_load(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Rezept laden."""
    await _async_run(hass, connection, msg, lambda c, m: c.load_recipe(m["name"]))


@websocket_api.websocket_command(v.ws_schema("library/delete", v.NAME_FIELDS))
@websocket_api.async_response
async def ws_library_delete(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Rezept aus der Bibliothek entfernen."""
    await _async_run(hass, connection, msg, lambda c, m: c.delete_recipe(m["name"]))


@websocket_api.websocket_command(v.ws_schema("library/import", v.IMPORT_FIELDS))
@websocket_api.require_admin
@websocket_api.async_response
async def ws_library_import(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Bibliothek aus v2.x übernehmen (nur Admin); Ergebnis ``{imported, skipped}``."""
    await _async_run(
        hass,
        connection,
        msg,
        lambda c, m: c.import_library(m["recipes"], m.get("active_recipe_name")),
        with_result=True,
    )


# ---------------------------------------------------------------------------
# Ablauf
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(v.ws_schema("control/start"))
@websocket_api.async_response
async def ws_control_start(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Brauvorgang starten."""
    await _async_run(hass, connection, msg, lambda c, _m: c.start())


@websocket_api.websocket_command(v.ws_schema("control/stop"))
@websocket_api.async_response
async def ws_control_stop(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Brauvorgang stoppen (nie eingeschränkt)."""
    await _async_run(hass, connection, msg, lambda c, _m: c.stop())


@websocket_api.websocket_command(v.ws_schema("control/next_rest"))
@websocket_api.async_response
async def ws_control_next_rest(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Zur nächsten Rast springen."""
    await _async_run(hass, connection, msg, lambda c, _m: c.next_rest())


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(v.WS_SETTINGS_UPDATE)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_settings_update(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Sicherheitsrelevante Einstellungen ändern (nur Admin)."""
    keys = tuple(str(key) for key in v.SETTINGS_FIELDS)
    await _async_run(
        hass, connection, msg, lambda c, m: c.async_update_settings(**_fields(m, *keys))
    )


@websocket_api.websocket_command(v.WS_DISPLAY_UPDATE)
@websocket_api.async_response
async def ws_display_update(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Sprache/Einheit ändern (alle Benutzer)."""
    keys = tuple(str(key) for key in v.DISPLAY_FIELDS)
    await _async_run(
        hass, connection, msg, lambda c, m: c.async_update_settings(**_fields(m, *keys))
    )


_COMMANDS = (
    ws_subscribe,
    ws_rest_add,
    ws_rest_update,
    ws_rest_delete,
    ws_rest_move,
    ws_recipe_clear,
    ws_library_save,
    ws_library_load,
    ws_library_delete,
    ws_library_import,
    ws_control_start,
    ws_control_stop,
    ws_control_next_rest,
    ws_settings_update,
    ws_display_update,
)


@callback
def async_register(hass: HomeAssistant) -> None:
    """Registriert alle Befehle (einmal pro HA-Prozess aus ``async_setup``)."""
    for command in _COMMANDS:
        websocket_api.async_register_command(hass, command)
