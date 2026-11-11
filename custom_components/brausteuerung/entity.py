"""Gemeinsame Basis der Entitäten (Design §8.5)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import DEVICE_NAME, DOMAIN
from .controller import BrauController


class BrauEntity(Entity):
    """Entität der Brausteuerung; aktualisiert sich als Listener des Controllers."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # Plattform der Entität; von jeder Unterklasse gesetzt (sensor, button, ...).
    _entity_platform: Platform

    def __init__(
        self, controller: BrauController, entry: ConfigEntry, description: EntityDescription
    ) -> None:
        self._controller = controller
        self.entity_description = description
        self._attr_translation_key = description.translation_key or description.key
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        # Stabile Entity-IDs: Mit ``has_entity_name`` würde HA die Entity-ID bei
        # einer Neuinstallation aus dem Gerätenamen bilden. Der Gerätename ist
        # aber der Anzeigename „HA-Beer-Brew-Control“, daraus entstünde z. B.
        # ``sensor.ha_beer_brew_control_status``. README, Karte und Automationen
        # der Benutzer verwenden jedoch ``sensor.brausteuerung_status`` usw.
        # Deshalb wird die Entity-ID explizit aus Domäne und Schlüssel
        # vorgeschlagen. HA übernimmt eine so gesetzte ``entity_id`` als
        # ``suggested_object_id``, die – anders als der aus dem Namen
        # abgeleitete ``object_id_base`` – nicht mit dem Gerätenamen präfixiert
        # wird. Der Vorschlag gilt nur bei der ersten Registrierung; bereits
        # registrierte (oder vom Benutzer umbenannte) Entity-IDs kommen weiter
        # aus der Entity-Registry über die unveränderte unique_id. Alternativ
        # hätte man den Gerätenamen technisch bei „Brausteuerung“ belassen
        # können, dann zeigte HA aber nicht den neuen Anzeigenamen.
        self.entity_id = f"{self._entity_platform}.{DOMAIN}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=DEVICE_NAME,
            manufacturer=DEVICE_NAME,
            model="Maischesteuerung",
            sw_version=controller.version,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Nach dem Shutdown des Controllers nicht mehr verfügbar."""
        return self._controller.loaded

    async def async_added_to_hass(self) -> None:
        """Als Listener registrieren; Abmelden beim Entfernen der Entität."""
        await super().async_added_to_hass()
        self.async_on_remove(self._controller.async_add_listener(self.async_write_ha_state))
