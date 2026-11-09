"""Texte der persistenten Benachrichtigungen (Design §7.9).

``brew_complete``, ``brew_complete_manual``, ``overtemp`` und ``comm_loss`` sind
1:1 aus ``automations.yaml`` (v2.x) übernommen, so wie YAML die gefalteten
Blöcke (``>-``) ausgibt. Platzhalter werden über ``str.format`` mit festen
Schlüsseln eingesetzt; Benutzerdaten stehen nie im Format-String selbst.
"""

from __future__ import annotations

from typing import Final

from .const import DEFAULT_LANGUAGE

# kind → Sprache → (Titel, Text)
MESSAGES: Final[dict[str, dict[str, tuple[str, str]]]] = {
    "brew_complete": {
        "en": ("🍺 Brewing complete!", "All rests have been completed successfully."),
        "de": ("🍺 Brauprozess abgeschlossen!", "Alle Raststufen wurden erfolgreich durchlaufen."),
    },
    "brew_complete_manual": {
        "en": ("🍺 Brewing complete!", "Manual advance on the last rest — brewing complete."),
        "de": (
            "🍺 Brauprozess abgeschlossen!",
            "Manueller Wechsel auf der letzten Raststufe — Brauprozess abgeschlossen.",
        ),
    },
    "overtemp": {
        "en": (
            "🔥 Over-temperature detected!",
            "The actual temperature ({value} {unit}) has exceeded the safety threshold "
            "({threshold} {unit} = setpoint + offset). The heater was switched off and "
            "the brewing process was paused. Please check the system before continuing.",
        ),
        "de": (
            "🔥 Übertemperatur erkannt!",
            "Die Ist-Temperatur ({value} {unit}) hat den Sicherheitsschwellwert "
            "({threshold} {unit} = Soll + Offset) überschritten. Die Heizung wurde "
            "ausgeschaltet und der Brauprozess angehalten (paused). Bitte Anlage prüfen, "
            "bevor fortgesetzt wird.",
        ),
    },
    "comm_loss": {
        "en": (
            "📡 Communication loss with temperature sensor!",
            "The temperature sensor ({sensor}) was unreachable for at least 30 s "
            "(unavailable/unknown). The heater was switched off for safety reasons. "
            "Please check the sensor and connection before continuing the brewing process.",
        ),
        "de": (
            "📡 Kommunikationsverlust zum Temperatursensor!",
            "Der Temperatursensor ({sensor}) war für mind. 30 s nicht erreichbar "
            "(unavailable/unknown). Die Heizung wurde aus Sicherheitsgründen "
            "ausgeschaltet. Bitte Sensor und Verbindung prüfen, bevor der Brauprozess "
            "fortgesetzt wird.",
        ),
    },
    "stale_sensor": {
        "en": (
            "📡 Stale sensor value",
            "The temperature sensor ({sensor}) has not reported a value for at least "
            "10 minutes while the heater was on. The heater was switched off and the "
            "brewing process was paused. Please check the sensor before continuing.",
        ),
        "de": (
            "📡 Sensorwert veraltet",
            "Der Temperatursensor ({sensor}) hat bei eingeschalteter Heizung seit "
            "mindestens 10 Minuten keinen Wert gemeldet. Die Heizung wurde ausgeschaltet "
            "und der Brauprozess angehalten (paused). Bitte Sensor prüfen, bevor "
            "fortgesetzt wird.",
        ),
    },
    "actuator_failure": {
        "en": (
            "⚠️ Heater actuator does not switch off!",
            "The heater ({heater}) still reports 'on' after a switch-off command. "
            "The command is being repeated. Please switch off the heater manually "
            "and check the actuator.",
        ),
        "de": (
            "⚠️ Heizungs-Aktor schaltet nicht ab!",
            "Die Heizung ({heater}) meldet nach einem Ausschaltbefehl weiterhin 'an'. "
            "Der Befehl wird wiederholt. Bitte die Heizung manuell ausschalten und den "
            "Aktor prüfen.",
        ),
    },
    "actuator_unavailable": {
        "en": (
            "⚠️ Heater actuator unavailable",
            "The heater ({heater}) has been unavailable for at least 30 s. "
            "The brewing process was paused. Please check the actuator and connection.",
        ),
        "de": (
            "⚠️ Heizungs-Aktor nicht erreichbar",
            "Die Heizung ({heater}) ist seit mindestens 30 s nicht erreichbar. "
            "Der Brauprozess wurde angehalten (paused). Bitte Aktor und Verbindung prüfen.",
        ),
    },
    "internal_error": {
        "en": (
            "⚠️ Internal error in HA-Beer-Brew-Control",
            "An internal error occurred. The heater was switched off for safety reasons "
            "and a running brewing process was paused. Details are in the log.",
        ),
        "de": (
            "⚠️ Interner Fehler in HA-Beer-Brew-Control",
            "Es ist ein interner Fehler aufgetreten. Die Heizung wurde aus "
            "Sicherheitsgründen ausgeschaltet und ein laufender Brauprozess angehalten "
            "(paused). Details stehen im Log.",
        ),
    },
}


def render(kind: str, language: str, **placeholders: str) -> tuple[str, str]:
    """Liefert ``(Titel, Text)`` in der Sprache der Integration."""
    texts = MESSAGES[kind]
    title, template = texts.get(language, texts[DEFAULT_LANGUAGE])
    return title, template.format(**placeholders)
