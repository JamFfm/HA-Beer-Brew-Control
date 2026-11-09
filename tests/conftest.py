"""Gemeinsame Fixtures für die Integrationstests (Design §13.1).

``http`` und ``websocket_api`` werden real eingerichtet (Abhängigkeiten aus dem
Manifest). Einen echten Port bindet ``http`` im Harness nicht: Das autouse
``disable_http_server`` von pytest-homeassistant-custom-component patcht
``HomeAssistantHTTP.async_bind``/``start``, und ``HomeAssistantHTTP.stop`` ist ohne
gestarteten Server ein No-op. Deshalb braucht auch der STOP-Test keinen eigenen
Patch und keine Spezial-Fixture (Review R4-1). ``frontend`` wird über
``mock_frontend`` nur vorgetäuscht, damit das ``home-assistant-frontend``-Paket
nicht nötig ist.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import persistent_notification
from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, UrlManager
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.brausteuerung.const import (
    CONF_HEATER_ENTITY,
    CONF_HYSTERESIS,
    CONF_LANGUAGE,
    CONF_SAFETY_OFFSET,
    CONF_SENSOR_ENTITY,
    CONF_UNIT,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from custom_components.brausteuerung.controller import BrauController

SENSOR = "sensor.mash"
HEATER = "switch.heater"

DEFAULT_OPTIONS: dict[str, Any] = {
    CONF_SENSOR_ENTITY: SENSOR,
    CONF_HEATER_ENTITY: HEATER,
    CONF_HYSTERESIS: 1.0,
    CONF_SAFETY_OFFSET: 10.0,
    CONF_LANGUAGE: "de",
    CONF_UNIT: "°C",
}

# Ein Rezept mit zwei Rasten (Langform wie im Store).
TWO_RESTS: list[dict[str, Any]] = [
    {"name": "Eiweißrast", "temperature": 55.0, "duration": 15},
    {"name": "Verzuckerung", "temperature": 65.0, "duration": 30},
]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Custom-Integrationen in jedem Test mit ``hass`` laden.

    Reine Logiktests (hypothesis) brauchen keine HA-Instanz; für sie wird
    ``enable_custom_integrations`` (und damit ``hass``) nicht angefordert.
    """
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")


@pytest.fixture
def mock_frontend(hass: HomeAssistant) -> None:
    """Täuscht ``frontend`` vor (Komponente gilt als geladen, URL-Manager vorhanden)."""
    hass.config.components.add("frontend")
    hass.data[DATA_EXTRA_MODULE_URL] = UrlManager(lambda *_: None, [])


class FakeSwitch:
    """Registriert ``switch.turn_on``/``turn_off`` und zeichnet Aufrufe auf.

    Modi (global über ``mode``, je Service über ``service_modes``, je Entity über
    ``entity_modes``, einmalig über ``queue``):
    - ``normal``: setzt den State der Entity.
    - ``stuck``: nimmt den Befehl an, ändert den State aber nicht.
    - ``raise``: wirft sofort ``HomeAssistantError`` (ohne Suspension).
    - ``hang``: wartet auf ``release`` und schaltet danach normal.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[tuple[str, str]] = []
        self.completed: list[tuple[str, str]] = []
        self.mode = "normal"
        self.service_modes: dict[str, str] = {}
        self.entity_modes: dict[str, str] = {}
        self.queue: list[str] = []
        self.release = asyncio.Event()

    def register(self) -> None:
        """Registriert beide Services."""
        self.hass.services.async_register("switch", "turn_on", self._handle)
        self.hass.services.async_register("switch", "turn_off", self._handle)

    def off_calls(self, entity_id: str = HEATER) -> int:
        """Anzahl der ``turn_off``-Aufrufe für ``entity_id``."""
        return sum(1 for call in self.calls if call == ("turn_off", entity_id))

    def on_calls(self, entity_id: str = HEATER) -> int:
        """Anzahl der ``turn_on``-Aufrufe für ``entity_id``."""
        return sum(1 for call in self.calls if call == ("turn_on", entity_id))

    def reset(self) -> None:
        """Löscht die Aufzeichnung."""
        self.calls.clear()
        self.completed.clear()

    def _mode_for(self, service: str, entity_id: str) -> str:
        if self.queue:
            return self.queue.pop(0)
        return self.entity_modes.get(entity_id, self.service_modes.get(service, self.mode))

    async def _handle(self, call: ServiceCall) -> None:
        entity_ids = call.data["entity_id"]
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        for entity_id in entity_ids:
            self.calls.append((call.service, entity_id))
            mode = self._mode_for(call.service, entity_id)
            if mode == "raise":
                raise HomeAssistantError("Aktor-Fehler (Test)")
            if mode == "hang":
                await self.release.wait()
            if mode != "stuck":
                state = self.hass.states.get(entity_id)
                attrs = dict(state.attributes) if state else {}
                self.hass.states.async_set(
                    entity_id, "on" if call.service == "turn_on" else "off", attrs
                )
            self.completed.append((call.service, entity_id))


@pytest.fixture
def fake_switch(hass: HomeAssistant) -> FakeSwitch:
    """Fake-Switch mit registrierten Services und ``switch.heater`` = off."""
    switch = FakeSwitch(hass)
    switch.register()
    hass.states.async_set(HEATER, "off")
    return switch


def set_sensor(hass: HomeAssistant, value: float | str, unit: str = "°C") -> None:
    """Setzt den Temperatursensor."""
    hass.states.async_set(SENSOR, str(value), {"unit_of_measurement": unit})


class NotificationLog(list[str]):
    """Liste der Benachrichtigungs-IDs; ``texts`` hält den letzten (Titel, Text) je ID."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: dict[str, tuple[str | None, str]] = {}


@pytest.fixture
def notifications(hass: HomeAssistant) -> Generator[NotificationLog]:
    """Zeichnet alle erzeugten persistenten Benachrichtigungen auf."""
    created = NotificationLog()
    original = persistent_notification.async_create

    def _record(
        hass_: HomeAssistant,
        message: str,
        title: str | None = None,
        notification_id: str | None = None,
    ) -> None:
        created.append(notification_id or "")
        created.texts[notification_id or ""] = (title, message)
        original(hass_, message, title, notification_id)

    with patch.object(persistent_notification, "async_create", _record):
        yield created


SetupFn = Callable[..., Awaitable[MockConfigEntry]]


@pytest.fixture
def setup_integration(
    hass: HomeAssistant, hass_storage: dict[str, Any], mock_frontend: None
) -> SetupFn:
    """Richtet die Integration über einen ``MockConfigEntry`` ein."""

    async def _setup(
        options: dict[str, Any] | None = None,
        stored: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        entry: MockConfigEntry | None = None,
    ) -> MockConfigEntry:
        if stored is not None:
            hass_storage[STORAGE_KEY] = {
                "version": STORAGE_VERSION,
                "minor_version": 1,
                "key": STORAGE_KEY,
                "data": stored,
            }
        if entry is None:
            entry = MockConfigEntry(
                domain=DOMAIN,
                title="HA-Beer-Brew-Control",
                options=DEFAULT_OPTIONS if options is None else options,
                data=data or {},
            )
            entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    return _setup


async def ws_command(client: Any, command: str, **payload: Any) -> dict[str, Any]:
    """Sendet ``brausteuerung/<command>`` und liefert die Antwort (``result``-Nachricht)."""
    await client.send_json_auto_id({"type": f"{DOMAIN}/{command}", **payload})
    return await client.receive_json()


def controller_of(entry: ConfigEntry) -> BrauController:
    """Controller des Entries."""
    return entry.runtime_data


async def advance(hass: HomeAssistant, freezer: Any, seconds: float, step: float = 5) -> None:
    """Spult die Zeit in Schritten vor und feuert die fälligen Timer."""
    remaining = seconds
    while remaining > 0:
        delta = min(step, remaining)
        freezer.tick(timedelta(seconds=delta))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        remaining -= delta
