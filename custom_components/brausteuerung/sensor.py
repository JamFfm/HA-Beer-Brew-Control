"""Sensoren der Brausteuerung (Design §8.5).

Die Temperatursensoren haben bewusst weder ``device_class`` noch ``state_class``:
Die Einheit ist reine Beschriftung, HA soll nicht umrechnen und keine
Langzeitstatistik führen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import BrauConfigEntry
from .const import STATUS_PAUSED, STATUS_RUNNING, STATUSES
from .controller import BrauController
from .entity import BrauEntity


def _current_rest(controller: BrauController) -> str | None:
    """Name der aktiven Rast (nur bei running/paused)."""
    if controller.status not in (STATUS_RUNNING, STATUS_PAUSED):
        return None
    recipe = controller.run_recipe
    index = controller.step_index
    return recipe[index].name if 0 <= index < len(recipe) else None


def _rest_count(controller: BrauController) -> int:
    if controller.status in (STATUS_RUNNING, STATUS_PAUSED):
        return len(controller.run_recipe)
    return len(controller.recipe)


def _safety_threshold(controller: BrauController) -> float | None:
    threshold = controller.safety_threshold
    return round(threshold, 2) if threshold is not None else None


@dataclass(frozen=True, kw_only=True)
class BrauSensorDescription(SensorEntityDescription):
    """Beschreibung eines Sensors samt Lesefunktionen."""

    value_fn: Callable[[BrauController], StateType | datetime]
    attrs_fn: Callable[[BrauController], dict[str, Any]] | None = None
    # Temperaturwert: Einheit vom Quellsensor, sonst die Anzeigeeinheit.
    temperature: bool = False


SENSORS: tuple[BrauSensorDescription, ...] = (
    BrauSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=list(STATUSES),
        value_fn=lambda c: c.status,
        attrs_fn=lambda c: {
            "phase": c.phase,
            "step_index": c.step_index,
            "rest_count": _rest_count(c),
        },
    ),
    BrauSensorDescription(
        key="current_rest",
        translation_key="current_rest",
        value_fn=_current_rest,
        attrs_fn=lambda c: {
            "index": c.step_index if c.status in (STATUS_RUNNING, STATUS_PAUSED) else None
        },
    ),
    BrauSensorDescription(
        key="target_temperature",
        translation_key="target_temperature",
        value_fn=lambda c: c.setpoint,
        temperature=True,
    ),
    BrauSensorDescription(
        key="safety_threshold",
        translation_key="safety_threshold",
        value_fn=_safety_threshold,
        temperature=True,
    ),
    BrauSensorDescription(
        key="hold_end",
        translation_key="hold_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.hold_ends_at,
    ),
    BrauSensorDescription(
        key="brew_started",
        translation_key="brew_started",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.started_at,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BrauConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Legt die Sensoren an."""
    controller = entry.runtime_data
    async_add_entities(BrauSensor(controller, entry, description) for description in SENSORS)


class BrauSensor(BrauEntity, SensorEntity):
    """Sensor, der einen Wert des Controllers zeigt."""

    _entity_platform = Platform.SENSOR
    entity_description: BrauSensorDescription

    @property
    def native_value(self) -> StateType | datetime:
        """Aktueller Wert."""
        return self.entity_description.value_fn(self._controller)

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Einheit des Quellsensors, sonst die Anzeigeeinheit (nur Temperaturwerte)."""
        if not self.entity_description.temperature:
            return None
        if (sensor := self._controller.sensor_entity) is not None and (
            state := self.hass.states.get(sensor)
        ) is not None:
            unit = state.attributes.get("unit_of_measurement")
            if isinstance(unit, str) and unit:
                return unit
        return self._controller.unit

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Zusätzliche Attribute (klein, recorder-tauglich)."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self._controller)
