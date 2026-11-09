"""Reine Logik der Brausteuerung (Design §5).

Port der fachlichen Funktionen aus ``www/brausteuerung-logic.js``. Dieses Modul
importiert nichts aus Home Assistant und hat keine Seiteneffekte. Alle
Rezeptoperationen sind immutabel und arbeiten auf ``tuple[Rest, ...]``.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .const import (
    BIDI_CONTROL_CHARS,
    DEFAULT_REST_NAME,
    LANGUAGES,
    MAX_DURATION_MIN,
    MAX_HYSTERESIS,
    MAX_LIBRARY_RECIPES,
    MAX_NAME_LENGTH,
    MAX_RESTS,
    MAX_SAFETY_OFFSET,
    MAX_TEMPERATURE,
    MIN_DURATION_MIN,
    MIN_HYSTERESIS,
    MIN_SAFETY_OFFSET,
    MIN_TEMPERATURE,
    MODIFIED_SUFFIX,
    PHASE_HEATING,
    PHASE_HOLDING,
    STATUS_PAUSED,
    STATUS_RUNNING,
    UNITS,
)

# Heizentscheidung von evaluate().
HEATER_ON: Final = "on"
HEATER_OFF: Final = "off"
HEATER_KEEP: Final = "keep"

# Unzulässige Unicode-Kategorien in Namen (Design §4.4).
_FORBIDDEN_CATEGORIES: Final = frozenset({"Cc", "Zl", "Zp"})

_ENTITY_ID_RE: Final = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+\Z")

# Entfernt ein bekanntes Suffix (beider Sprachen) samt optionaler Zählnummer,
# aber nur mit führendem Leerraum (JS-Parität, logic.js Z. 712–713).
_MODIFIED_TAIL_RE: Final = re.compile(
    r"\s+(?:"
    + "|".join(re.escape(s) for s in MODIFIED_SUFFIX.values())
    + r")(?:\s+[0-9]+)?\Z",
    re.IGNORECASE,
)


class LogicError(Exception):
    """Fachlicher Fehler; ``code`` ist der Übersetzungsschlüssel unter ``exceptions``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# Validierung
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    """Prüft auf int/float ohne bool."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _check_name(value: str) -> str:
    """Prüft Länge und Zeichen eines bereits getrimmten Namens."""
    if len(value) > MAX_NAME_LENGTH:
        raise LogicError("invalid_name")
    for char in value:
        if char in BIDI_CONTROL_CHARS or unicodedata.category(char) in _FORBIDDEN_CATEGORIES:
            raise LogicError("invalid_name")
    return value


def validate_temperature(value: Any) -> float:
    """Solltemperatur: endliche Zahl 0–100 (kein bool) → float."""
    if not _is_number(value) or not math.isfinite(value):
        raise LogicError("invalid_temperature")
    if not MIN_TEMPERATURE <= value <= MAX_TEMPERATURE:
        raise LogicError("invalid_temperature")
    return float(value)


def validate_duration(value: Any) -> int:
    """Haltezeit in Minuten: ganze Zahl 1–1440 (kein bool, kein float)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise LogicError("invalid_duration")
    if not MIN_DURATION_MIN <= value <= MAX_DURATION_MIN:
        raise LogicError("invalid_duration")
    return value


def validate_rest_name(value: Any, position: int) -> str:
    """Rastname: optional; leer → ``"Rast {position}"`` (Port von resolveStepName)."""
    if value is None:
        return DEFAULT_REST_NAME.format(n=position)
    if not isinstance(value, str):
        raise LogicError("invalid_name")
    stripped = value.strip()
    if stripped == "":
        return DEFAULT_REST_NAME.format(n=position)
    return _check_name(stripped)


def validate_recipe_name(value: Any, allow_empty: bool = True) -> str:
    """Rezeptname: getrimmt, höchstens 64 Zeichen, keine Steuerzeichen."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise LogicError("invalid_name")
    stripped = value.strip()
    if stripped == "":
        if allow_empty:
            return ""
        raise LogicError("invalid_name")
    return _check_name(stripped)


def validate_hysteresis(value: Any) -> float:
    """Hystereseband (0, 5], Parität zu ``isValidHysteresis``."""
    if not _is_number(value) or not math.isfinite(value):
        raise LogicError("invalid_hysteresis")
    if not MIN_HYSTERESIS < value <= MAX_HYSTERESIS:
        raise LogicError("invalid_hysteresis")
    return float(value)


def validate_offset(value: Any) -> float:
    """Sicherheits-Offset [0, 20]."""
    if not _is_number(value) or not math.isfinite(value):
        raise LogicError("invalid_offset")
    if not MIN_SAFETY_OFFSET <= value <= MAX_SAFETY_OFFSET:
        raise LogicError("invalid_offset")
    return float(value)


def validate_language(value: Any) -> str:
    """Sprache ``en`` oder ``de``."""
    if not isinstance(value, str) or value not in LANGUAGES:
        raise LogicError("invalid_language")
    return value


def validate_unit(value: Any) -> str:
    """Anzeigeeinheit ``°C`` oder ``°F`` (reine Beschriftung)."""
    if not isinstance(value, str) or value not in UNITS:
        raise LogicError("invalid_unit")
    return value


def validate_entity_id(value: Any, domain: str) -> str:
    """Entity-ID der Form ``<domain>.<objekt>`` (nur Kleinbuchstaben, Ziffern, ``_``)."""
    if (
        not isinstance(value, str)
        or not _ENTITY_ID_RE.match(value)
        or not value.startswith(f"{domain}.")
    ):
        raise LogicError("invalid_entity")
    return value


# ---------------------------------------------------------------------------
# Datentypen
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rest:
    """Eine Raststufe (Name, Solltemperatur in Anzeigeeinheit, Dauer in Minuten)."""

    name: str
    temperature: float
    duration: int

    def to_dict(self) -> dict[str, Any]:
        """Serialisierbare Langform."""
        return {"name": self.name, "temperature": self.temperature, "duration": self.duration}

    @classmethod
    def from_dict(cls, data: Any, position: int = 1) -> Rest:
        """Erzeugt eine validierte Rast; wirft ``LogicError`` bei ungültigen Werten."""
        if not isinstance(data, Mapping):
            raise LogicError("invalid_temperature")
        return cls(
            name=validate_rest_name(data.get("name"), position),
            temperature=validate_temperature(data.get("temperature")),
            duration=validate_duration(data.get("duration")),
        )


def recipe_from_list(raw: Any) -> tuple[Rest, ...]:
    """Validiert eine Liste von Rast-Dicts (Langform) zu einem Rezept."""
    if not isinstance(raw, list | tuple):
        raise LogicError("invalid_index")
    if len(raw) > MAX_RESTS:
        raise LogicError("too_many_rests")
    return tuple(Rest.from_dict(item, pos) for pos, item in enumerate(raw, start=1))


def recipe_to_list(recipe: Iterable[Rest]) -> list[dict[str, Any]]:
    """Rezept als Liste von Dicts (Langform)."""
    return [rest.to_dict() for rest in recipe]


@dataclass(frozen=True, slots=True)
class LibraryRecipe:
    """Ein benanntes Rezept der Bibliothek."""

    name: str
    steps: tuple[Rest, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialisierbare Form ``{"name": …, "steps": [...]}``."""
        return {"name": self.name, "steps": recipe_to_list(self.steps)}

    @classmethod
    def from_dict(cls, data: Any) -> LibraryRecipe:
        """Erzeugt einen validierten Bibliothekseintrag."""
        if not isinstance(data, Mapping):
            raise LogicError("invalid_name")
        return cls(
            name=validate_recipe_name(data.get("name"), allow_empty=False),
            steps=recipe_from_list(data.get("steps")),
        )


# ---------------------------------------------------------------------------
# Sensorwert
# ---------------------------------------------------------------------------


def parse_sensor_value(state: str | None) -> float | None:
    """Sensor-State → endliche Zahl oder ``None`` (Port von ``isSensorValid``)."""
    if not isinstance(state, str):
        return None
    trimmed = state.strip()
    if trimmed == "" or trimmed.lower() in ("unknown", "unavailable"):
        return None
    # Python akzeptiert "1_0" als Zahl, JS ``Number`` nicht.
    if "_" in trimmed:
        return None
    try:
        value = float(trimmed)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


# ---------------------------------------------------------------------------
# Rezeptoperationen
# ---------------------------------------------------------------------------


def _check_index(recipe: Sequence[Rest], index: Any) -> int:
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(recipe):
        raise LogicError("invalid_index")
    return index


def add_rest(recipe: Sequence[Rest], rest: Rest) -> tuple[Rest, ...]:
    """Hängt eine Rast an; höchstens ``MAX_RESTS``."""
    if len(recipe) >= MAX_RESTS:
        raise LogicError("too_many_rests")
    return (*recipe, rest)


def update_rest(recipe: Sequence[Rest], index: Any, rest: Rest) -> tuple[Rest, ...]:
    """Ersetzt die Rast an ``index``."""
    idx = _check_index(recipe, index)
    return tuple(rest if i == idx else item for i, item in enumerate(recipe))


def delete_rest(recipe: Sequence[Rest], index: Any) -> tuple[Rest, ...]:
    """Entfernt die Rast an ``index``, die Reihenfolge der übrigen bleibt."""
    idx = _check_index(recipe, index)
    return tuple(item for i, item in enumerate(recipe) if i != idx)


def move_rest(recipe: Sequence[Rest], index: Any, direction: Any) -> tuple[Rest, ...]:
    """Vertauscht die Rast mit ihrem Nachbarn (``-1`` hoch, ``+1`` runter)."""
    if isinstance(direction, bool) or direction not in (-1, 1):
        raise LogicError("invalid_index")
    idx = _check_index(recipe, index)
    target = idx + direction
    if not 0 <= target < len(recipe):
        raise LogicError("invalid_index")
    items = list(recipe)
    items[idx], items[target] = items[target], items[idx]
    return tuple(items)


# ---------------------------------------------------------------------------
# Legacy (v2.x, input_text.brau_rezept_json)
# ---------------------------------------------------------------------------


def parse_legacy_recipe(raw: str | None) -> tuple[Rest, ...] | None:
    """Parst das alte Rezept-JSON (kompakt ``n/t/d`` oder lang).

    Jede Rast wird validiert; ein einziger Fehler verwirft das ganze Rezept (``None``).
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, list) or len(parsed) > MAX_RESTS:
        return None
    rests: list[Rest] = []
    for position, item in enumerate(parsed, start=1):
        if not isinstance(item, Mapping):
            return None
        long_form = {
            "name": item["n"] if "n" in item else item.get("name"),
            "temperature": item["t"] if "t" in item else item.get("temperature"),
            "duration": item["d"] if "d" in item else item.get("duration"),
        }
        try:
            rests.append(Rest.from_dict(long_form, position))
        except LogicError:
            return None
    return tuple(rests)


# ---------------------------------------------------------------------------
# Regelung
# ---------------------------------------------------------------------------


def hysteresis_decision(ist: float, soll: float, prev_on: bool, hyst: float) -> bool:
    """Zweipunktregelung: unter ``soll - hyst`` AN, ab ``soll`` AUS, dazwischen unverändert."""
    if ist < soll - hyst:
        return True
    if ist >= soll:
        return False
    return prev_on


def safety_threshold(soll: float, offset: float) -> float:
    """Übertemperaturschwelle ``soll + offset`` (ungerundet)."""
    return soll + offset


def next_step(index: int, length: int) -> tuple[int, bool]:
    """Stufenübergang: ``(index + 1, False)`` oder ``(index, True)`` nach der letzten Rast."""
    if index + 1 < length:
        return index + 1, False
    return index, True


@dataclass(frozen=True, slots=True)
class EvalInput:
    """Eingabe einer Auswertung."""

    status: str
    phase: str | None
    setpoint: float | None
    sensor_value: float | None
    heater_on: bool
    hysteresis: float
    offset: float


@dataclass(frozen=True, slots=True)
class Decision:
    """Ergebnis einer Auswertung."""

    heater: str = HEATER_KEEP
    pause: bool = False
    overtemp: bool = False
    start_hold: bool = False
    sensor_invalid: bool = False


def evaluate(inp: EvalInput) -> Decision:
    """Kernentscheidung, Sicherheit zuerst (Design §5, Reihenfolge 1–6)."""
    # 1. Ohne laufenden oder pausierten Ablauf wird nichts entschieden.
    if inp.status not in (STATUS_RUNNING, STATUS_PAUSED):
        return Decision()
    # 2. Ungültiger Sensorwert: aus, solange running oder die Heizung an ist.
    if inp.sensor_value is None:
        off = inp.status == STATUS_RUNNING or inp.heater_on
        return Decision(heater=HEATER_OFF if off else HEATER_KEEP, sensor_invalid=True)
    ist = inp.sensor_value
    # 3. Übertemperatur (auch in paused).
    if inp.setpoint is not None and ist > safety_threshold(inp.setpoint, inp.offset):
        return Decision(heater=HEATER_OFF, overtemp=True, pause=inp.status == STATUS_RUNNING)
    # 4. Pausiert: keine Regelung.
    if inp.status == STATUS_PAUSED:
        return Decision()
    # Fail-safe: running ohne Sollwert oder mit unbekannter Phase → aus.
    if inp.setpoint is None:
        return Decision(heater=HEATER_OFF)
    soll = inp.setpoint
    # 5. Aufheizen; bei Erreichen Hold starten und mit derselben Messung regeln.
    if inp.phase == PHASE_HEATING:
        if ist < soll:
            return Decision(heater=HEATER_ON)
        on = hysteresis_decision(ist, soll, inp.heater_on, inp.hysteresis)
        return Decision(heater=HEATER_ON if on else HEATER_OFF, start_hold=True)
    # 6. Halten per Hysterese.
    if inp.phase == PHASE_HOLDING:
        on = hysteresis_decision(ist, soll, inp.heater_on, inp.hysteresis)
        return Decision(heater=HEATER_ON if on else HEATER_OFF)
    return Decision(heater=HEATER_OFF)


# ---------------------------------------------------------------------------
# Bibliothek
# ---------------------------------------------------------------------------


def find_recipe(library: Iterable[LibraryRecipe], name: Any) -> LibraryRecipe | None:
    """Sucht einen Eintrag über den getrimmten Namen (case-sensitive)."""
    if not isinstance(name, str):
        return None
    key = name.strip()
    if key == "":
        return None
    for entry in library:
        if entry.name == key:
            return entry
    return None


def upsert_recipe(
    library: Sequence[LibraryRecipe], recipe: LibraryRecipe
) -> tuple[LibraryRecipe, ...]:
    """Gleicher Name → gleiche Position ersetzen, sonst anhängen (``library_full``)."""
    for idx, entry in enumerate(library):
        if entry.name == recipe.name:
            return tuple(recipe if i == idx else item for i, item in enumerate(library))
    if len(library) >= MAX_LIBRARY_RECIPES:
        raise LogicError("library_full")
    return (*library, recipe)


def remove_recipe(library: Sequence[LibraryRecipe], name: Any) -> tuple[LibraryRecipe, ...]:
    """Entfernt den Eintrag; unbekannt → ``recipe_not_found``."""
    entry = find_recipe(library, name)
    if entry is None:
        raise LogicError("recipe_not_found")
    return tuple(item for item in library if item.name != entry.name)


def validate_import(raw_list: Any) -> tuple[list[LibraryRecipe], int]:
    """Validiert importierte Einträge.

    Ungültige Einträge und doppelte Namen (der erste gewinnt) zählen als ``skipped``.
    Vorhandene Namen und die Obergrenze prüft der Aufrufer gegen seine Bibliothek.
    Ein Wert, der keine Liste ist, ergibt ``([], 0)``.
    """
    if not isinstance(raw_list, list | tuple):
        return [], 0
    valid: list[LibraryRecipe] = []
    names: set[str] = set()
    skipped = 0
    for item in raw_list:
        try:
            recipe = LibraryRecipe.from_dict(item)
        except LogicError:
            skipped += 1
            continue
        if recipe.name in names:
            skipped += 1
            continue
        names.add(recipe.name)
        valid.append(recipe)
    return valid, skipped


# ---------------------------------------------------------------------------
# Rezeptname
# ---------------------------------------------------------------------------


def is_recipe_unchanged(
    library: Iterable[LibraryRecipe], name: Any, steps: Iterable[Rest]
) -> bool:
    """``True``, wenn ein gleichnamiger Eintrag existiert und die Rasten identisch sind."""
    entry = find_recipe(library, name)
    return entry is not None and entry.steps == tuple(steps)


def next_modified_name(library: Iterable[LibraryRecipe], name: Any, suffix: str) -> str:
    """Name für ein abgewandeltes Rezept (Port von ``nextModifiedRecipeName``).

    Entfernt ein vorhandenes Suffix beider Sprachen (nur mit führendem Leerraum),
    hängt ``suffix`` an und zählt ab 2 hoch, solange der gekürzte Kandidat in der
    Bibliothek belegt ist oder dem Ausgangsnamen entspricht. Ergebnis ≤ 64 Zeichen.
    """
    base = name.strip() if isinstance(name, str) else ""
    if base == "":
        return ""
    entries = tuple(library)
    root = _MODIFIED_TAIL_RE.sub("", base, count=1).strip() or base
    n = 1
    while True:
        tail = f" {suffix}" if n == 1 else f" {suffix} {n}"
        candidate = root[: MAX_NAME_LENGTH - len(tail)].rstrip() + tail
        if find_recipe(entries, candidate) is None and candidate != base:
            return candidate
        n += 1
