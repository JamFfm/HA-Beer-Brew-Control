"""Auslieferung der Lovelace-Karte über einen statischen Pfad (Design §9).

Die Karte und ihr Logikmodul liegen in ``frontend/`` und werden unter
``/brausteuerung_frontend/`` ausgeliefert. ``add_extra_js_url`` bindet die Karte
in jedes Dashboard ein (YAML- und Storage-Modus), ohne Benutzerdaten zu ändern.
Der Cache-Buster ist ``?v=<Manifest-Version>``; die Karte lädt das Logikmodul
mit derselben Query nach.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import CARD_FILENAME, DATA_FRONTEND_REGISTERED, DOMAIN, URL_BASE

_LOGGER = logging.getLogger(__name__)

# Fester Pfad, nie benutzergesteuert (S10).
FRONTEND_DIR = Path(__file__).parent / "frontend"


async def async_register_card(hass: HomeAssistant) -> None:
    """Registriert statischen Pfad und Modul-URL einmal pro HA-Prozess.

    Fehler sind nicht fatal: Die Regelung braucht die Karte nicht. Der Merker
    wird auch nach einem Fehler gesetzt, damit ein Reload denselben Fehler nicht
    erneut loggt.
    """
    if hass.data.get(DATA_FRONTEND_REGISTERED):
        return
    try:
        version = str((await async_get_integration(hass, DOMAIN)).version)
        try:
            await hass.http.async_register_static_paths(
                [StaticPathConfig(URL_BASE, str(FRONTEND_DIR), cache_headers=False)]
            )
        except RuntimeError:
            _LOGGER.error(
                "Statischer Pfad %s ist bereits belegt; die Karte wird nicht eingebunden",
                URL_BASE,
            )
            return
        try:
            add_extra_js_url(hass, f"{URL_BASE}/{CARD_FILENAME}?v={version}")
        except KeyError:
            _LOGGER.warning(
                "Frontend nicht verfügbar; die Karte wird nicht automatisch eingebunden"
            )
    finally:
        hass.data[DATA_FRONTEND_REGISTERED] = True
