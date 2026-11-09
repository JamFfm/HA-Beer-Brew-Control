"""Config Flow (AK 9, AK 10; Design §8.1)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from conftest import DEFAULT_OPTIONS, SENSOR, FakeSwitch
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.brausteuerung.const import DOMAIN

VALID: dict[str, Any] = dict(DEFAULT_OPTIONS)


@pytest.fixture(autouse=True)
def _frontend(mock_frontend: None) -> None:
    """Der Flow lädt die Integration samt Abhängigkeit ``frontend``."""


def _suggested(result: dict[str, Any]) -> dict[str, Any]:
    """Vorbelegung (suggested_value) des angezeigten Formulars."""
    return {
        str(key): (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
    }


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_create_entry(
    hass: HomeAssistant, mock_frontend: None, fake_switch: FakeSwitch
) -> None:
    """Gültige Eingaben → Entry mit Optionen, ohne Legacy-Daten."""
    result = await _start(hass)
    assert result["errors"] == {}
    suggested = _suggested(result)
    assert suggested["hysteresis"] == 1.0
    assert suggested["safety_offset"] == 10.0
    assert suggested["language"] == "en"
    assert suggested["unit"] == "°C"
    assert suggested["sensor_entity"] is None

    result = await hass.config_entries.flow.async_configure(result["flow_id"], VALID)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "HA-Beer-Brew-Control"
    assert result["data"] == {}
    entry = result["result"]
    assert dict(entry.options) == VALID
    assert entry.runtime_data.sensor_entity == SENSOR


async def test_prefill_from_legacy(
    hass: HomeAssistant, mock_frontend: None, fake_switch: FakeSwitch
) -> None:
    """Validierte v2.x-Werte belegen vor; Rezept und Name gehen nach ``legacy_import``."""
    recipe = [{"n": "Eiweißrast", "t": 55, "d": 15}]
    for entity_id, state in {
        "input_text.brau_sensor_entity": SENSOR,
        "input_text.brau_heater_entity": "light.falsch",  # ungültig → keine Vorbelegung
        "input_number.brau_hysterese": "0.5",
        "input_number.brau_sicherheits_offset": "8.0",
        "input_select.brau_language": "de",
        "input_select.brau_unit": "°F",
        "input_text.brau_rezept_json": json.dumps(recipe),
        "input_text.brau_rezept_name": "Helles",
    }.items():
        hass.states.async_set(entity_id, state)

    result = await _start(hass)
    assert _suggested(result) == {
        "sensor_entity": SENSOR,
        "heater_entity": None,
        "hysteresis": 0.5,
        "safety_offset": 8.0,
        "language": "de",
        "unit": "°F",
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**VALID, "language": "de", "unit": "°F"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    # Die Übernahme läuft beim Setup und leert data danach.
    assert entry.data == {}
    controller = entry.runtime_data
    assert controller.recipe_name == "Helles"
    assert [rest.name for rest in controller.recipe] == ["Eiweißrast"]


async def test_legacy_import_payload(hass: HomeAssistant) -> None:
    """``data`` enthält ``legacy_import`` (Setup gepatcht, damit data erhalten bleibt)."""
    hass.states.async_set("input_text.brau_rezept_name", "Helles")
    result = await _start(hass)
    with patch("custom_components.brausteuerung.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], VALID)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"legacy_import": {"recipe_name": "Helles"}}
    assert result["options"] == VALID


async def test_single_instance(hass: HomeAssistant) -> None:
    """Zweiter Flow bricht mit ``single_instance_allowed`` ab (AK 9)."""
    MockConfigEntry(domain=DOMAIN, options=DEFAULT_OPTIONS).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("heater_entity", "light.x"),
        ("sensor_entity", "switch.x"),
        ("hysteresis", 0),
        ("hysteresis", 6),
        ("safety_offset", 21),
        ("safety_offset", -1),
        ("language", "fr"),
        ("unit", "K"),
    ],
)
async def test_selector_errors(hass: HomeAssistant, field: str, value: Any) -> None:
    """Selektor-Stufe: ``InvalidData`` mit dem Feld in ``schema_errors`` (AK 10)."""
    result = await _start(hass)
    with pytest.raises(InvalidData) as exc:
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**VALID, field: value}
        )
    assert field in exc.value.schema_errors


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("sensor_entity", "uuid", "invalid_sensor"),
        ("heater_entity", "uuid", "invalid_heater"),
        ("hysteresis", "NaN", "invalid_hysteresis"),
        ("safety_offset", "NaN", "invalid_offset"),
    ],
)
async def test_step_errors(hass: HomeAssistant, field: str, value: Any, error: str) -> None:
    """Step-Stufe: Registry-UUID und NaN → Feldfehler, Formular erneut (AK 10)."""
    if value == "uuid":
        domain = "sensor" if field == "sensor_entity" else "switch"
        value = er.async_get(hass).async_get_or_create(domain, "test", f"uid_{field}").id
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**VALID, field: value}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: error}
    # Die Eingaben bleiben im Formular erhalten.
    assert _suggested(result)["language"] == VALID["language"]
    assert not hass.config_entries.async_entries(DOMAIN)
