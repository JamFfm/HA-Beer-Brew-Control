"""Property- und Beispieltests für logic.py (Design §13.1, Port der JS-Properties)."""

from __future__ import annotations

import json
import math
import re

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from custom_components.brausteuerung import logic
from custom_components.brausteuerung.const import (
    MAX_LIBRARY_RECIPES,
    MAX_NAME_LENGTH,
    MAX_RESTS,
    MODIFIED_SUFFIX,
)
from custom_components.brausteuerung.logic import (
    HEATER_KEEP,
    HEATER_OFF,
    HEATER_ON,
    Decision,
    EvalInput,
    LibraryRecipe,
    LogicError,
    Rest,
)

# ---------------------------------------------------------------------------
# Strategien
# ---------------------------------------------------------------------------

_name_chars = st.characters(
    codec="utf-8",
    exclude_categories=("Cc", "Zl", "Zp", "Cs"),
    exclude_characters="".join(logic.BIDI_CONTROL_CHARS),
)

valid_names = (
    st.text(_name_chars, min_size=1, max_size=MAX_NAME_LENGTH)
    .map(str.strip)
    .filter(lambda s: 0 < len(s) <= MAX_NAME_LENGTH)
)
temperatures = st.floats(min_value=0, max_value=100, allow_nan=False) | st.integers(0, 100)
durations = st.integers(1, 1440)
rests = st.builds(
    Rest,
    name=valid_names,
    temperature=temperatures.map(float),
    duration=durations,
)
recipes = st.lists(rests, max_size=MAX_RESTS).map(tuple)


@st.composite
def libraries(draw: st.DrawFn, max_size: int = 10) -> tuple[LibraryRecipe, ...]:
    """Bibliothek mit eindeutigen Namen."""
    names = draw(st.lists(valid_names, max_size=max_size, unique=True))
    return tuple(LibraryRecipe(n, draw(st.lists(rests, max_size=3).map(tuple))) for n in names)


# ---------------------------------------------------------------------------
# P1: Roundtrip to_dict/from_dict
# ---------------------------------------------------------------------------


@given(rests)
def test_p1_rest_roundtrip(rest: Rest) -> None:
    assert Rest.from_dict(rest.to_dict()) == rest
    # Auch über JSON (Store) bleibt die Rast gleich.
    assert Rest.from_dict(json.loads(json.dumps(rest.to_dict()))) == rest


@given(valid_names, recipes)
def test_p1_library_recipe_roundtrip(name: str, steps: tuple[Rest, ...]) -> None:
    recipe = LibraryRecipe(name, steps)
    assert LibraryRecipe.from_dict(json.loads(json.dumps(recipe.to_dict()))) == recipe


def test_recipe_longer_than_255_chars_roundtrip() -> None:
    """AK 22: Rezepte mit mehr als 255 Zeichen Serialisierung sind gültig."""
    steps = tuple(Rest(f"Rast mit langem Namen Nummer {i}", 62.5, 30) for i in range(MAX_RESTS))
    serialized = json.dumps(logic.recipe_to_list(steps))
    assert len(serialized) > 255
    assert logic.recipe_from_list(json.loads(serialized)) == steps
    lib = LibraryRecipe("Langes Rezept", steps)
    assert LibraryRecipe.from_dict(json.loads(json.dumps(lib.to_dict()))) == lib


# ---------------------------------------------------------------------------
# P3: Validierung Temperatur/Dauer
# ---------------------------------------------------------------------------


@given(temperatures)
def test_p3_valid_temperature(value: float) -> None:
    assert logic.validate_temperature(value) == float(value)


@given(
    st.floats(allow_nan=True, allow_infinity=True).filter(
        lambda v: not (0 <= v <= 100) or math.isnan(v)
    )
    | st.integers().filter(lambda v: not 0 <= v <= 100)
    | st.sampled_from([True, False, None, "50", [50]])
)
def test_p3_invalid_temperature(value: object) -> None:
    with pytest.raises(LogicError) as exc:
        logic.validate_temperature(value)
    assert exc.value.code == "invalid_temperature"


@given(durations)
def test_p3_valid_duration(value: int) -> None:
    assert logic.validate_duration(value) == value


@given(
    st.integers().filter(lambda v: not 1 <= v <= 1440)
    | st.floats(allow_nan=True)
    | st.sampled_from([True, False, None, "15", 15.0, 1.5])
)
def test_p3_invalid_duration(value: object) -> None:
    with pytest.raises(LogicError) as exc:
        logic.validate_duration(value)
    assert exc.value.code == "invalid_duration"


# ---------------------------------------------------------------------------
# P4: Standardname
# ---------------------------------------------------------------------------


@given(st.integers(1, MAX_RESTS), st.text(" \t\u3000", max_size=5) | st.none())
def test_p4_default_rest_name(position: int, value: str | None) -> None:
    assert logic.validate_rest_name(value, position) == f"Rast {position}"


@given(st.integers(1, MAX_RESTS), valid_names)
def test_p4_non_empty_name_kept(position: int, name: str) -> None:
    assert logic.validate_rest_name(f"  {name} ", position) == name


def test_names_too_long_or_wrong_type() -> None:
    with pytest.raises(LogicError):
        logic.validate_rest_name("a" * 65, 1)
    with pytest.raises(LogicError):
        logic.validate_recipe_name("a" * 65)
    with pytest.raises(LogicError):
        logic.validate_rest_name(5, 1)
    with pytest.raises(LogicError):
        logic.validate_recipe_name("  ", allow_empty=False)
    assert logic.validate_recipe_name(" ") == ""
    assert logic.validate_recipe_name(None) == ""
    assert logic.validate_recipe_name("a" * 64) == "a" * 64


@pytest.mark.parametrize(
    "char",
    ["\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069"],
)
def test_bidi_control_chars_rejected(char: str) -> None:
    with pytest.raises(LogicError) as exc:
        logic.validate_recipe_name(f"Hel{char}les")
    assert exc.value.code == "invalid_name"
    with pytest.raises(LogicError):
        logic.validate_rest_name(f"Rast{char}", 1)


@pytest.mark.parametrize("char", ["\n", "\x00", "\x1b", "\u2028", "\u2029"])
def test_control_chars_rejected(char: str) -> None:
    with pytest.raises(LogicError):
        logic.validate_recipe_name(f"Hel{char}les")


def test_zwj_emoji_and_variation_selector_accepted() -> None:
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert logic.validate_recipe_name(f"Bier {family}") == f"Bier {family}"
    assert logic.validate_rest_name("Rast \u2764\ufe0f", 1) == "Rast \u2764\ufe0f"


# ---------------------------------------------------------------------------
# P5/P6: add/update/delete
# ---------------------------------------------------------------------------


@given(st.lists(rests, max_size=MAX_RESTS - 1).map(tuple), rests)
def test_p5_add_appends(recipe: tuple[Rest, ...], rest: Rest) -> None:
    result = logic.add_rest(recipe, rest)
    assert len(result) == len(recipe) + 1
    assert result[:-1] == recipe
    assert result[-1] == rest


@given(st.lists(rests, min_size=MAX_RESTS, max_size=MAX_RESTS).map(tuple), rests)
def test_p5_add_limit(recipe: tuple[Rest, ...], rest: Rest) -> None:
    with pytest.raises(LogicError) as exc:
        logic.add_rest(recipe, rest)
    assert exc.value.code == "too_many_rests"


@given(st.lists(rests, min_size=1, max_size=MAX_RESTS).map(tuple), rests, st.data())
def test_p6_update_replaces_only_index(
    recipe: tuple[Rest, ...], rest: Rest, data: st.DataObject
) -> None:
    index = data.draw(st.integers(0, len(recipe) - 1))
    result = logic.update_rest(recipe, index, rest)
    assert len(result) == len(recipe)
    assert result[index] == rest
    assert all(result[i] == recipe[i] for i in range(len(recipe)) if i != index)


@given(st.lists(rests, min_size=1, max_size=MAX_RESTS).map(tuple), st.data())
def test_p6_delete_keeps_order(recipe: tuple[Rest, ...], data: st.DataObject) -> None:
    index = data.draw(st.integers(0, len(recipe) - 1))
    result = logic.delete_rest(recipe, index)
    assert result == recipe[:index] + recipe[index + 1 :]


@given(recipes, st.integers() | st.sampled_from([True, None, 1.0, "0"]))
def test_p6_invalid_index(recipe: tuple[Rest, ...], index: object) -> None:
    is_int = isinstance(index, int) and not isinstance(index, bool)
    assume(not (is_int and 0 <= index < len(recipe)))
    rest = Rest("x", 50.0, 10)
    for call in (
        lambda: logic.update_rest(recipe, index, rest),
        lambda: logic.delete_rest(recipe, index),
        lambda: logic.move_rest(recipe, index, 1),
    ):
        with pytest.raises(LogicError) as exc:
            call()
        assert exc.value.code == "invalid_index"


# ---------------------------------------------------------------------------
# P14: move = Permutation + Grenzen
# ---------------------------------------------------------------------------


@given(st.lists(rests, min_size=1, max_size=MAX_RESTS).map(tuple), st.data())
def test_p14_move_is_adjacent_transposition(recipe: tuple[Rest, ...], data: st.DataObject) -> None:
    index = data.draw(st.integers(0, len(recipe) - 1))
    direction = data.draw(st.sampled_from([-1, 1]))
    target = index + direction
    if not 0 <= target < len(recipe):
        with pytest.raises(LogicError) as exc:
            logic.move_rest(recipe, index, direction)
        assert exc.value.code == "invalid_index"
        return
    result = logic.move_rest(recipe, index, direction)
    assert sorted(result, key=repr) == sorted(recipe, key=repr)
    assert result[index] == recipe[target]
    assert result[target] == recipe[index]
    assert all(result[i] == recipe[i] for i in range(len(recipe)) if i not in (index, target))


@pytest.mark.parametrize("direction", [0, 2, -2, True, None, "1"])
def test_p14_invalid_direction(direction: object) -> None:
    recipe = (Rest("a", 50.0, 1), Rest("b", 60.0, 1))
    with pytest.raises(LogicError):
        logic.move_rest(recipe, 0, direction)


# ---------------------------------------------------------------------------
# P7: Hysterese inkl. Monotonie
# ---------------------------------------------------------------------------

_temps = st.floats(min_value=-20, max_value=120, allow_nan=False)
_hyst = st.floats(min_value=0.01, max_value=5, allow_nan=False)


@given(_temps, _temps, st.booleans(), _hyst)
def test_p7_hysteresis_rules(ist: float, soll: float, prev: bool, hyst: float) -> None:
    result = logic.hysteresis_decision(ist, soll, prev, hyst)
    if ist < soll - hyst:
        assert result is True
    elif ist >= soll:
        assert result is False
    else:
        assert result is prev


@given(_temps, _temps, _temps, st.booleans(), _hyst)
def test_p7_hysteresis_monotonic(a: float, b: float, soll: float, prev: bool, hyst: float) -> None:
    low, high = sorted((a, b))
    # Ist bei höherer Temperatur AN, dann auch bei niedrigerer.
    if logic.hysteresis_decision(high, soll, prev, hyst):
        assert logic.hysteresis_decision(low, soll, prev, hyst)


@pytest.mark.parametrize("prev", [True, False])
def test_p7_hysteresis_examples(prev: bool) -> None:
    assert logic.hysteresis_decision(63.9, 65.0, prev, 1.0) is True
    assert logic.hysteresis_decision(65.0, 65.0, prev, 1.0) is False
    assert logic.hysteresis_decision(64.5, 65.0, prev, 1.0) is prev


# ---------------------------------------------------------------------------
# P8: next_step
# ---------------------------------------------------------------------------


@given(st.integers(1, MAX_RESTS), st.data())
def test_p8_next_step(length: int, data: st.DataObject) -> None:
    index = data.draw(st.integers(0, length - 1))
    new_index, done = logic.next_step(index, length)
    if index + 1 < length:
        assert (new_index, done) == (index + 1, False)
    else:
        assert (new_index, done) == (index, True)


# ---------------------------------------------------------------------------
# P9: Sicherheitsinvariante von evaluate
# ---------------------------------------------------------------------------

eval_inputs = st.builds(
    EvalInput,
    status=st.sampled_from(["idle", "running", "paused", "done"]),
    phase=st.sampled_from(["heating", "holding", None]),
    setpoint=st.none() | st.floats(0, 100, allow_nan=False),
    sensor_value=st.none() | st.floats(-50, 200, allow_nan=False),
    heater_on=st.booleans(),
    hysteresis=st.floats(0.1, 5, allow_nan=False),
    offset=st.floats(0, 20, allow_nan=False),
)


@given(eval_inputs)
def test_p9_danger_never_on(inp: EvalInput) -> None:
    decision = logic.evaluate(inp)
    sensor_bad = inp.sensor_value is None
    overtemp = (
        not sensor_bad
        and inp.setpoint is not None
        and inp.sensor_value > logic.safety_threshold(inp.setpoint, inp.offset)
    )
    if inp.status not in ("running", "paused"):
        assert decision == Decision()
        return
    if sensor_bad or overtemp:
        assert decision.heater != HEATER_ON
    if sensor_bad:
        assert decision.sensor_invalid
        if inp.status == "running" or inp.heater_on:
            assert decision.heater == HEATER_OFF
    elif overtemp:
        assert decision.heater == HEATER_OFF
        assert decision.overtemp
        assert decision.pause == (inp.status == "running")
    elif inp.status == "paused":
        assert decision.heater == HEATER_KEEP


def _inp(**kwargs: object) -> EvalInput:
    base: dict[str, object] = {
        "status": "running",
        "phase": "heating",
        "setpoint": 65.0,
        "sensor_value": 50.0,
        "heater_on": False,
        "hysteresis": 1.0,
        "offset": 10.0,
    }
    base.update(kwargs)
    return EvalInput(**base)  # type: ignore[arg-type]


def test_evaluate_examples() -> None:
    assert logic.evaluate(_inp()) == Decision(heater=HEATER_ON)
    assert logic.evaluate(_inp(sensor_value=65.0)) == Decision(heater=HEATER_OFF, start_hold=True)
    assert logic.evaluate(_inp(phase="holding", sensor_value=63.9)).heater == HEATER_ON
    assert (
        logic.evaluate(_inp(phase="holding", sensor_value=64.5, heater_on=True)).heater == HEATER_ON
    )
    assert logic.evaluate(_inp(phase="holding", sensor_value=65.0)).heater == HEATER_OFF
    assert logic.evaluate(_inp(sensor_value=75.1)) == Decision(
        heater=HEATER_OFF, overtemp=True, pause=True
    )
    assert logic.evaluate(_inp(status="paused", sensor_value=75.1)) == Decision(
        heater=HEATER_OFF, overtemp=True, pause=False
    )
    assert logic.evaluate(_inp(status="paused", sensor_value=40.0)) == Decision()
    assert logic.evaluate(_inp(sensor_value=None)) == Decision(
        heater=HEATER_OFF, sensor_invalid=True
    )
    assert logic.evaluate(_inp(status="paused", sensor_value=None)) == Decision(sensor_invalid=True)
    assert logic.evaluate(_inp(status="idle", sensor_value=None)) == Decision()
    # Fail-safe für inkonsistenten Zustand.
    assert logic.evaluate(_inp(setpoint=None)).heater == HEATER_OFF
    assert logic.evaluate(_inp(phase=None)).heater == HEATER_OFF


# ---------------------------------------------------------------------------
# P13: Schwelle ungerundet
# ---------------------------------------------------------------------------


@given(st.floats(0, 100, allow_nan=False), st.floats(0, 20, allow_nan=False))
def test_p13_safety_threshold(soll: float, offset: float) -> None:
    assert logic.safety_threshold(soll, offset) == soll + offset


def test_p13_threshold_not_rounded() -> None:
    assert logic.safety_threshold(64.123, 10.456) == 64.123 + 10.456


# ---------------------------------------------------------------------------
# P15: Hysterese-/Offset-Validierung, weitere Validatoren
# ---------------------------------------------------------------------------


@given(st.floats(min_value=0, max_value=5, exclude_min=True))
def test_p15_valid_hysteresis(value: float) -> None:
    assert logic.validate_hysteresis(value) == value


@given(
    st.floats(allow_nan=True).filter(lambda v: math.isnan(v) or not 0 < v <= 5)
    | st.sampled_from([0, True, None, "1.0"])
)
def test_p15_invalid_hysteresis(value: object) -> None:
    with pytest.raises(LogicError) as exc:
        logic.validate_hysteresis(value)
    assert exc.value.code == "invalid_hysteresis"


@given(st.floats(min_value=0, max_value=20) | st.integers(0, 20))
def test_valid_offset(value: float) -> None:
    assert logic.validate_offset(value) == float(value)


@given(
    st.floats(allow_nan=True).filter(lambda v: math.isnan(v) or not 0 <= v <= 20)
    | st.sampled_from([True, None, "10"])
)
def test_invalid_offset(value: object) -> None:
    with pytest.raises(LogicError) as exc:
        logic.validate_offset(value)
    assert exc.value.code == "invalid_offset"


def test_language_unit_entity() -> None:
    assert logic.validate_language("de") == "de"
    assert logic.validate_unit("°F") == "°F"
    for bad in ("fr", "", None, 1):
        with pytest.raises(LogicError, match="invalid_language"):
            logic.validate_language(bad)
    for bad in ("K", "C", None):
        with pytest.raises(LogicError, match="invalid_unit"):
            logic.validate_unit(bad)
    assert logic.validate_entity_id("switch.heater", "switch") == "switch.heater"
    for bad in ("light.x", "switch.", "switch.Heater", "switch.a.b", "0f3a", None, "switch.a\n"):
        with pytest.raises(LogicError, match="invalid_entity"):
            logic.validate_entity_id(bad, "switch")


# ---------------------------------------------------------------------------
# P12: parse_sensor_value
# ---------------------------------------------------------------------------


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_p12_numeric_states(value: float) -> None:
    assert logic.parse_sensor_value(repr(value)) == value
    assert logic.parse_sensor_value(f"  {value!r} ") == value


@pytest.mark.parametrize(
    "state", [None, "", "  ", "unknown", "unavailable", "UNKNOWN", "abc", "nan", "inf", "1_0", 5]
)
def test_p12_invalid_states(state: object) -> None:
    assert logic.parse_sensor_value(state) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_legacy_recipe
# ---------------------------------------------------------------------------


def test_parse_legacy_compact_and_long() -> None:
    compact = '[{"n":"Eiweißrast","t":55,"d":15},{"n":"","t":63.5,"d":45}]'
    assert logic.parse_legacy_recipe(compact) == (
        Rest("Eiweißrast", 55.0, 15),
        Rest("Rast 2", 63.5, 45),
    )
    long = '[{"name":"Maltose","temperature":63,"duration":30}]'
    assert logic.parse_legacy_recipe(long) == (Rest("Maltose", 63.0, 30),)
    assert logic.parse_legacy_recipe("[]") == ()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        " ",
        "kaputt",
        "{}",
        '[{"n":"a","t":101,"d":10}]',
        '[{"n":"a","t":50,"d":10},{"n":"b","t":50,"d":0}]',
        '[{"n":"a","t":50,"d":1.5}]',
        '[{"n":"a","t":true,"d":10}]',
        "[1]",
        json.dumps([{"n": "a", "t": 50, "d": 1}] * (MAX_RESTS + 1)),
    ],
)
def test_parse_legacy_invalid(raw: str | None) -> None:
    assert logic.parse_legacy_recipe(raw) is None


@given(recipes)
def test_parse_legacy_roundtrip_compact(recipe: tuple[Rest, ...]) -> None:
    raw = json.dumps([{"n": r.name, "t": r.temperature, "d": r.duration} for r in recipe])
    assert logic.parse_legacy_recipe(raw) == recipe


# ---------------------------------------------------------------------------
# P16: Bibliotheksoperationen
# ---------------------------------------------------------------------------


@given(libraries(), valid_names, recipes)
def test_p16_upsert(library: tuple[LibraryRecipe, ...], name: str, steps: tuple[Rest, ...]) -> None:
    recipe = LibraryRecipe(name, steps)
    result = logic.upsert_recipe(library, recipe)
    existing = [i for i, r in enumerate(library) if r.name == name]
    if existing:
        assert len(result) == len(library)
        assert result[existing[0]] == recipe
        assert all(result[i] == library[i] for i in range(len(library)) if i != existing[0])
    else:
        assert result == (*library, recipe)
    assert logic.find_recipe(result, f" {name} ") == recipe


def test_p16_library_full() -> None:
    library = tuple(LibraryRecipe(f"R{i}", ()) for i in range(MAX_LIBRARY_RECIPES))
    with pytest.raises(LogicError) as exc:
        logic.upsert_recipe(library, LibraryRecipe("neu", ()))
    assert exc.value.code == "library_full"
    # Überschreiben bleibt bei voller Bibliothek möglich.
    replaced = logic.upsert_recipe(library, LibraryRecipe("R5", (Rest("x", 1.0, 1),)))
    assert replaced[5].steps == (Rest("x", 1.0, 1),)


@given(libraries(), st.data())
def test_p16_remove(library: tuple[LibraryRecipe, ...], data: st.DataObject) -> None:
    if library:
        victim = data.draw(st.sampled_from(library))
        result = logic.remove_recipe(library, victim.name)
        assert result == tuple(r for r in library if r is not victim)
        assert logic.find_recipe(result, victim.name) is None
    with pytest.raises(LogicError) as exc:
        logic.remove_recipe(library, "\u0000nicht vorhanden")
    assert exc.value.code == "recipe_not_found"


def test_find_recipe_edge_cases() -> None:
    library = (LibraryRecipe("Helles", ()),)
    assert logic.find_recipe(library, "") is None
    assert logic.find_recipe(library, None) is None
    assert logic.find_recipe(library, "helles") is None


def test_validate_import() -> None:
    raw = [
        {"name": "Helles", "steps": [{"name": "", "temperature": 63, "duration": 30}]},
        {"name": "Helles", "steps": []},
        {"name": "", "steps": []},
        {"name": "Kaputt", "steps": [{"name": "a", "temperature": 200, "duration": 1}]},
        {"name": "Ohne Steps"},
        "kein dict",
        {"name": "Bock", "steps": []},
    ]
    valid, skipped = logic.validate_import(raw)
    assert valid == [
        LibraryRecipe("Helles", (Rest("Rast 1", 63.0, 30),)),
        LibraryRecipe("Bock", ()),
    ]
    assert skipped == 5
    assert logic.validate_import("keine Liste") == ([], 0)


# ---------------------------------------------------------------------------
# Rezeptname: is_recipe_unchanged, next_modified_name
# ---------------------------------------------------------------------------


def test_is_recipe_unchanged() -> None:
    steps = (Rest("a", 50.0, 10),)
    library = (LibraryRecipe("Helles", steps),)
    assert logic.is_recipe_unchanged(library, "Helles", steps)
    assert logic.is_recipe_unchanged(library, " Helles ", list(steps))
    assert not logic.is_recipe_unchanged(library, "Helles", (Rest("a", 50.0, 11),))
    assert not logic.is_recipe_unchanged(library, "Bock", steps)


_SUFFIX_TOKEN_END = re.compile(r"(?:^|\s)(?:new|neu)(?:\s+[0-9]+)?\Z", re.IGNORECASE)
_word_names = st.lists(
    st.sampled_from(["Helles", "Bock", "Neu", "New", "neu", "NEW", "2", "17", "a", "Ä", "🍺"]),
    min_size=1,
    max_size=6,
).map(" ".join)


@given(
    libraries(),
    _word_names | valid_names.filter(lambda s: len(s) <= 50),
    st.sampled_from(["en", "de"]),
)
def test_next_modified_name_single_suffix(
    library: tuple[LibraryRecipe, ...], name: str, language: str
) -> None:
    suffix = MODIFIED_SUFFIX[language]
    root = re.sub(r"\s+(?:New|Neu)(?:\s+[0-9]+)?\Z", "", name, count=1, flags=re.IGNORECASE)
    root = root.strip()
    assume(root != "" and not _SUFFIX_TOKEN_END.search(root))
    result = logic.next_modified_name(library, name, suffix)
    match = re.fullmatch(rf"(.*) {re.escape(suffix)}(?: ([0-9]+))?", result, re.DOTALL)
    assert match is not None
    if match.group(2) is not None:
        assert int(match.group(2)) >= 2
    assert not _SUFFIX_TOKEN_END.search(match.group(1))
    assert logic.find_recipe(library, result) is None
    assert result != name.strip()


@given(libraries(), valid_names, st.sampled_from(["en", "de"]))
def test_next_modified_name_length_and_collision(
    library: tuple[LibraryRecipe, ...], name: str, language: str
) -> None:
    result = logic.next_modified_name(library, name, MODIFIED_SUFFIX[language])
    assert 0 < len(result) <= MAX_NAME_LENGTH
    assert logic.find_recipe(library, result) is None
    assert result != name
    assert logic.validate_recipe_name(result, allow_empty=False) == result


def test_next_modified_name_examples() -> None:
    assert logic.next_modified_name((), "Helles", "Neu") == "Helles Neu"
    assert logic.next_modified_name((), "Helles", "New") == "Helles New"
    assert logic.next_modified_name((LibraryRecipe("Helles Neu", ()),), "Helles", "Neu") == (
        "Helles Neu 2"
    )
    assert logic.next_modified_name((), "Helles Neu", "Neu") == "Helles Neu 2"
    assert logic.next_modified_name((), "Helles New", "Neu") == "Helles Neu"
    assert logic.next_modified_name((), "", "Neu") == ""
    assert logic.next_modified_name((), "   ", "Neu") == ""
    assert logic.next_modified_name((), None, "Neu") == ""


def test_next_modified_name_suffix_only_names() -> None:
    """JS-Parität: Ohne führenden Leerraum bleibt der ganze Name Wurzel."""
    assert logic.next_modified_name((), "Neu", "Neu") == "Neu Neu"
    assert logic.next_modified_name((), "neu 2", "Neu") == "neu 2 Neu"


def test_next_modified_name_truncation() -> None:
    assert logic.next_modified_name((), "a" * 64, "Neu") == "a" * 60 + " Neu"
    library = (LibraryRecipe("a" * 60 + " Neu", ()),)
    assert logic.next_modified_name(library, "a" * 64, "Neu") == "a" * 58 + " Neu 2"
    name = "a" * 59 + " bcd"
    assert len(name) == 63
    assert logic.next_modified_name((), name, "Neu") == "a" * 59 + " Neu"
