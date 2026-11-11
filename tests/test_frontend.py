"""Auslieferung der Karte (Design §9, §13.1; AK 7, AK 8, S10)."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from conftest import SetupFn, set_sensor
from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from yarl import URL

from custom_components.brausteuerung.card_registration import async_register_card
from custom_components.brausteuerung.const import DATA_FRONTEND_REGISTERED

CARD_URL = "/brausteuerung_frontend/brausteuerung-card.js"
LOGIC_URL = "/brausteuerung_frontend/brausteuerung-logic.js"
MODULE_URL = f"{CARD_URL}?v=3.0.1"


@pytest.fixture
async def loaded(hass: HomeAssistant, setup_integration: SetupFn, fake_switch: Any) -> Any:
    """Integration mit Sensor eingerichtet."""
    set_sensor(hass, 50.0)
    return await setup_integration()


async def test_card_and_logic_are_served(
    hass: HomeAssistant, loaded: Any, hass_client: Any
) -> None:
    """Karte und Logikmodul sind unter dem statischen Pfad erreichbar (AK 7)."""
    client = await hass_client()
    resp = await client.get(CARD_URL)
    assert resp.status == 200
    assert 'const VERSION = "3.0.1"' in await resp.text()
    resp = await client.get(f"{CARD_URL}?v=3.0.1")
    assert resp.status == 200
    resp = await client.get(LOGIC_URL)
    assert resp.status == 200
    assert "export function translate" in await resp.text()


@pytest.mark.parametrize(
    "path",
    [
        "/brausteuerung_frontend/../manifest.json",
        "/brausteuerung_frontend/%2e%2e/manifest.json",
        "/brausteuerung_frontend/%2e%2e/%2e%2e/configuration.yaml",
        "/brausteuerung_frontend/..%2fmanifest.json",
    ],
)
async def test_no_path_traversal(
    hass: HomeAssistant, loaded: Any, hass_client: Any, path: str
) -> None:
    """Der statische Pfad liefert nichts außerhalb von ``frontend/`` (S10)."""
    client = await hass_client()
    # Rohpfad ohne Normalisierung durch den Client senden (encoded=True), damit
    # die Punktsegmente wirklich beim Server ankommen.
    base = str(client.make_url("/")).rstrip("/")
    resp = await client.session.get(URL(base + path, encoded=True))
    assert resp.status != 200
    assert '"domain": "brausteuerung"' not in await resp.text()


async def test_module_url_registered(hass: HomeAssistant, loaded: Any) -> None:
    """Die Karte wird mit Versions-Query als Extra-Modul eingebunden."""
    assert MODULE_URL in hass.data[DATA_EXTRA_MODULE_URL].urls
    assert hass.data[DATA_FRONTEND_REGISTERED] is True


async def test_reload_does_not_register_again(hass: HomeAssistant, loaded: Any) -> None:
    """Reload und erneuter Aufruf registrieren weder Pfad noch URL ein zweites Mal (AK 8)."""
    with (
        patch.object(hass.http, "async_register_static_paths", AsyncMock()) as register,
        patch(
            "custom_components.brausteuerung.card_registration.add_extra_js_url"
        ) as add_url,
    ):
        assert await hass.config_entries.async_reload(loaded.entry_id)
        await hass.async_block_till_done()
        await async_register_card(hass)
    register.assert_not_called()
    add_url.assert_not_called()
    assert [url for url in hass.data[DATA_EXTRA_MODULE_URL].urls if CARD_URL in url] == [
        MODULE_URL
    ]


async def test_missing_frontend_logs_warning(
    hass: HomeAssistant, hass_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Ohne ``frontend`` (KeyError) nur eine Warnung; der statische Pfad steht trotzdem."""
    assert await async_setup_component(hass, "http", {})
    assert DATA_EXTRA_MODULE_URL not in hass.data
    with caplog.at_level(logging.WARNING):
        await async_register_card(hass)
    assert "Frontend nicht verfügbar" in caplog.text
    assert hass.data[DATA_FRONTEND_REGISTERED] is True
    client = await hass_client()
    resp = await client.get(CARD_URL)
    assert resp.status == 200


async def test_static_path_taken_logs_error(
    hass: HomeAssistant, mock_frontend: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Belegter Pfad (RuntimeError) → Fehler im Log, keine Modul-URL, Merker gesetzt."""
    assert await async_setup_component(hass, "http", {})
    with (
        patch.object(
            hass.http, "async_register_static_paths", AsyncMock(side_effect=RuntimeError)
        ),
        caplog.at_level(logging.ERROR),
    ):
        await async_register_card(hass)
    assert "bereits belegt" in caplog.text
    assert MODULE_URL not in hass.data[DATA_EXTRA_MODULE_URL].urls
    assert hass.data[DATA_FRONTEND_REGISTERED] is True
