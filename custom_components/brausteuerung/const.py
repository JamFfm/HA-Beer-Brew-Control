"""Konstanten der Brausteuerung (Design §4.4, §4.6)."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "brausteuerung"
# Anzeigename der Integration (Gerät, Config-Entry-Titel). Die technische
# Domäne bleibt ``brausteuerung``; die Entity-IDs sind davon unabhängig (entity.py).
DEVICE_NAME: Final = "HA-Beer-Brew-Control"

PLATFORMS: Final[list[Platform]] = [
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]

# ---------------------------------------------------------------------------
# Speicher
# ---------------------------------------------------------------------------
STORAGE_KEY: Final = "brausteuerung.data"
STORAGE_VERSION: Final = 1

# ---------------------------------------------------------------------------
# Optionsschlüssel (entry.options) und Defaults
# ---------------------------------------------------------------------------
CONF_SENSOR_ENTITY: Final = "sensor_entity"
CONF_HEATER_ENTITY: Final = "heater_entity"
CONF_HYSTERESIS: Final = "hysteresis"
CONF_SAFETY_OFFSET: Final = "safety_offset"
CONF_LANGUAGE: Final = "language"
CONF_UNIT: Final = "unit"

DEFAULT_HYSTERESIS: Final = 1.0
DEFAULT_SAFETY_OFFSET: Final = 10.0
DEFAULT_LANGUAGE: Final = "en"
DEFAULT_UNIT: Final = "°C"

LANGUAGES: Final = ("en", "de")
UNITS: Final = ("°C", "°F")

# ---------------------------------------------------------------------------
# Grenzen (A11 final)
# ---------------------------------------------------------------------------
MAX_RESTS: Final = 30
MAX_LIBRARY_RECIPES: Final = 100
MAX_NAME_LENGTH: Final = 64
MIN_DURATION_MIN: Final = 1
MAX_DURATION_MIN: Final = 1440
MIN_TEMPERATURE: Final = 0.0
MAX_TEMPERATURE: Final = 100.0
# Interne Validierung (0, 5]; Eingabeschicht ab 0,1 (Design §4.4).
MIN_HYSTERESIS: Final = 0.0
MIN_HYSTERESIS_INPUT: Final = 0.1
MAX_HYSTERESIS: Final = 5.0
MIN_SAFETY_OFFSET: Final = 0.0
MAX_SAFETY_OFFSET: Final = 20.0

# Bidi-Steuerzeichen, die Namen optisch umdrehen können (U+202A–U+202E, U+2066–U+2069).
BIDI_CONTROL_CHARS: Final = frozenset(
    {chr(cp) for cp in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))}
)

# ---------------------------------------------------------------------------
# Laufzeitzustand
# ---------------------------------------------------------------------------
STATUS_IDLE: Final = "idle"
STATUS_RUNNING: Final = "running"
STATUS_PAUSED: Final = "paused"
STATUS_DONE: Final = "done"
STATUSES: Final = (STATUS_IDLE, STATUS_RUNNING, STATUS_PAUSED, STATUS_DONE)

PHASE_HEATING: Final = "heating"
PHASE_HOLDING: Final = "holding"

# ---------------------------------------------------------------------------
# Zeiten und Watchdogs
# ---------------------------------------------------------------------------
COMM_LOSS_TIMEOUT: Final = timedelta(seconds=30)
CONTROL_INTERVAL: Final = timedelta(seconds=5)
STALE_SENSOR_TIMEOUT: Final = timedelta(minutes=10)
ACTUATOR_VERIFY_DELAY: Final = timedelta(seconds=15)
ACTUATOR_MAX_RETRIES: Final = 3
ACTUATOR_UNAVAILABLE_TIMEOUT: Final = timedelta(seconds=30)
# Timeout eines einzelnen Service-Aufrufs an den Aktor in Sekunden.
SERVICE_CALL_TIMEOUT: Final = 10
# Mindestabstand der Tick-Retries nach fehlgeschlagenem Initial-OFF (§7.1 Schritt 3).
INITIAL_OFF_RETRY_INTERVAL: Final = timedelta(seconds=60)
# Shutdown: OFF-Aufruf und Abwarten laufender Aktor-Tasks, zusammen 10 s
# (deutlich unter HAs STOPPING_STAGE_SHUTDOWN_TIMEOUT = 20 s).
SHUTDOWN_OFF_TIMEOUT: Final = 5
SHUTDOWN_TASK_TIMEOUT: Final = 5

# Gründe für ein erzwungenes OFF über _request_heater (§7.2). Der Watchdog-Retry
# gehört bewusst nicht dazu: Er ruft _async_turn_off direkt auf (Review R4-4).
FORCED_OFF_REASONS: Final = frozenset(
    {
        "startup",
        "stop",
        "shutdown",
        "ha_stop",
        "unload",
        "setup_failed",
        "settings_old_heater",
    }
)

# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------
URL_BASE: Final = "/brausteuerung_frontend"
CARD_FILENAME: Final = "brausteuerung-card.js"
LOGIC_FILENAME: Final = "brausteuerung-logic.js"
DATA_FRONTEND_REGISTERED: Final = f"{DOMAIN}_frontend_registered"
DATA_ACTIVE: Final = f"{DOMAIN}_active"

# ---------------------------------------------------------------------------
# Namen
# ---------------------------------------------------------------------------
DEFAULT_REST_NAME: Final = "Rast {n}"
MODIFIED_SUFFIX: Final = {"en": "New", "de": "Neu"}

# ---------------------------------------------------------------------------
# Migration und Altlasten (v2.x)
# ---------------------------------------------------------------------------
# Die 13 Helfer aus configuration.yaml der v2.x.
LEGACY_HELPERS: Final = (
    "input_text.brau_rezept_json",
    "input_text.brau_rezept_name",
    "input_text.brau_sensor_entity",
    "input_text.brau_heater_entity",
    "input_select.brau_status",
    "input_select.brau_language",
    "input_select.brau_unit",
    "input_number.brau_aktuelle_stufe",
    "input_number.brau_solltemperatur",
    "input_number.brau_sicherheits_offset",
    "input_number.brau_hysterese",
    "timer.brau_raststufe",
    "input_button.brau_naechste_rast",
)
LEGACY_AUTOMATION_IDS: Final = frozenset(
    {
        "brausteuerung_raststufe",
        "brausteuerung_notaus",
        "brausteuerung_manueller_wechsel",
        "brausteuerung_uebertemperatur",
        "brausteuerung_komm_verlust",
    }
)
# Rückfall ohne ``id``-Attribut: die aus den alten Aliasen abgeleiteten Entity-IDs.
# Bewusst exakt statt Präfix, damit eigene ``automation.brausteuerung_*`` des
# Benutzers (die die neue Integration bedienen) den Start nicht sperren.
LEGACY_AUTOMATION_ENTITY_IDS: Final = frozenset(
    {
        "automation.brausteuerung_raststufe",
        "automation.brausteuerung_notaus",
        "automation.brausteuerung_manueller_stufenwechsel",
        "automation.brausteuerung_ubertemperaturschutz",
        "automation.brausteuerung_kommunikationsverlust",
    }
)

NOTIFICATION_ID_PREFIX: Final = "brausteuerung_"
