"""Brausteuerung: Maischesteuerung als Home-Assistant-Integration (Design §7.1, §9)."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from . import services, websocket
from .card_registration import async_register_card
from .const import DATA_ACTIVE, DOMAIN, PLATFORMS
from .controller import BrauController, BrauError
from .storage import BrauStore

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type BrauConfigEntry = ConfigEntry[BrauController]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Einmal pro HA-Prozess: WebSocket-Befehle, Services und Karte."""
    websocket.async_register(hass)
    services.async_register(hass)
    await async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BrauConfigEntry) -> bool:
    """Richtet den einzigen Controller ein."""
    if hass.data.get(DATA_ACTIVE) is not None:
        _LOGGER.error("Es läuft bereits ein Controller der Brausteuerung")
        return False
    hass.data[DATA_ACTIVE] = entry.entry_id
    try:
        store = BrauStore(hass)
        data = await store.async_load()
        version = str((await async_get_integration(hass, DOMAIN)).version)
        controller = BrauController(hass, entry, store, data, version)
        legacy_import = entry.data.get("legacy_import")
        try:
            await controller.async_import_legacy(legacy_import)
        except BrauError:
            _LOGGER.warning("Übernahme der v2.x-Daten fehlgeschlagen; nächster Versuch beim Setup")
        else:
            if entry.data and not store.read_only:
                hass.config_entries.async_update_entry(entry, data={})
        entry.runtime_data = controller
    except BaseException:
        hass.data.pop(DATA_ACTIVE, None)
        raise

    try:
        await controller.async_start()
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        _LOGGER.exception("Setup der Brausteuerung fehlgeschlagen; Heizung wird ausgeschaltet")
        await controller.async_shutdown("setup_failed")
        hass.data.pop(DATA_ACTIVE, None)
        raise
    return True


async def _async_update_listener(hass: HomeAssistant, entry: BrauConfigEntry) -> None:
    """Options-Änderung live übernehmen (kein Reload, Design §8.1)."""
    entry.runtime_data.apply_options(dict(entry.options))


async def async_unload_entry(hass: HomeAssistant, entry: BrauConfigEntry) -> bool:
    """Entlädt; das OFF kommt auch bei fehlschlagendem Plattform-Unload."""
    try:
        ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    finally:
        await entry.runtime_data.async_shutdown("unload")
        hass.data.pop(DATA_ACTIVE, None)
    return ok
