// @vitest-environment jsdom
//
// Unit-/Mock-Tests der Karte als dünner WebSocket-Client (v3.0.0, Design §13.2).
//
// -----------------------------------------------------------------------------
// Lade-Strategie / Einschränkung
// -----------------------------------------------------------------------------
// `brausteuerung-card.js` wartet beim Laden auf `ha-panel-lovelace` und leitet
// seine Basisklasse daraus ab:
//
//   await customElements.whenDefined("ha-panel-lovelace");
//   const LitElement = Object.getPrototypeOf(customElements.get("ha-panel-lovelace"));
//
// Unter Node/jsdom existiert weder HA noch das echte LitElement. Wir laden das
// echte Card-Modul dennoch, indem wir VOR dem (dynamischen) Import ein
// Fake-`ha-panel-lovelace` registrieren, dessen Prototyp-Kette eine minimale
// Fake-LitElement-Basisklasse mit `html`-/`css`-Tag-Funktionen bereitstellt.
//
// Das echte LitElement-Rendering (Shadow-DOM, reaktive Updates) steht damit
// NICHT zur Verfügung. Stattdessen liefert unsere `html`-Tag-Funktion einen
// flach zusammengesetzten String des gerenderten Templates. Render-Tests prüfen
// daher den TEXTINHALT des Templates. Die Aktionen sind reine JS-Logik und
// werden direkt auf der ECHTEN Card-Klasse gegen ein gemocktes `hass` getestet
// (`callWS`-Spy, `connection.subscribeMessage`, `user.is_admin`, `callService`).
// Die Ladereihenfolge ohne vorab definiertes Panel prüft card-load-order.test.js.
// -----------------------------------------------------------------------------

import { describe, it, expect, beforeAll, afterEach, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";

const CARD_PATH = "../../custom_components/brausteuerung/frontend/brausteuerung-card.js";
const CARD_FILE = path.resolve(
  process.cwd(),
  "custom_components/brausteuerung/frontend/brausteuerung-card.js"
);

// Sollwert-Sensor der Integration (aus der Entity-Registry, Snapshot.entities).
const SETPOINT_ENTITY = "sensor.brausteuerung_target_temperature";

// -----------------------------------------------------------------------------
// Fake-LitElement-Basis: flacht das gerenderte Template zu einem durchsuchbaren
// String ab. Event-Handler (Funktionen) werden ausgelassen; verschachtelte
// `html`-Ergebnisse und Arrays werden rekursiv zusammengeführt.
// -----------------------------------------------------------------------------
function renderValue(value) {
  if (value == null || value === false) return "";
  if (Array.isArray(value)) return value.map(renderValue).join("");
  if (typeof value === "object" && "_html" in value) return value._html;
  if (typeof value === "function") return ""; // Event-Handler etc.
  return String(value);
}

function htmlTag(strings, ...values) {
  let out = "";
  for (let i = 0; i < strings.length; i++) {
    out += strings[i];
    if (i < values.length) out += renderValue(values[i]);
  }
  return { _html: out };
}

function cssTag() {
  // Styles werden in den Tests nicht ausgewertet.
  return "";
}

class FakeLitElement extends HTMLElement {
  // Lebenszyklus-Hooks des echten LitElement als No-op, damit `super.…()` in
  // der Karte funktioniert.
  connectedCallback() {}
  disconnectedCallback() {}
  requestUpdate() {}
}
FakeLitElement.prototype.html = htmlTag;
FakeLitElement.prototype.css = cssTag;

// ha-panel-lovelace so registrieren, dass Object.getPrototypeOf(...) auf
// FakeLitElement zeigt.
class FakeHaPanelLovelace extends FakeLitElement {}

let CardClass;
let cardModule;

beforeAll(async () => {
  if (!customElements.get("ha-panel-lovelace")) {
    customElements.define("ha-panel-lovelace", FakeHaPanelLovelace);
  }
  cardModule = await import(CARD_PATH);
  CardClass = customElements.get("brausteuerung-card");
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

// -----------------------------------------------------------------------------
// Hilfsfunktionen
// -----------------------------------------------------------------------------

/** Snapshot der Integration wie von `brausteuerung/subscribe` (Design §7.7). */
function makeSnapshot(overrides = {}) {
  const { settings, ...rest } = overrides;
  return {
    version: "3.0.1",
    loaded: true,
    status: "idle",
    phase: null,
    step_index: 0,
    rest_count: 1,
    hold_ends_at: null,
    started_at: null,
    setpoint: null,
    safety_threshold: null,
    recipe: [{ name: "Maischen", temperature: 67, duration: 60 }],
    recipe_name: "",
    library: [],
    entities: { setpoint: SETPOINT_ENTITY },
    ...rest,
    settings: {
      sensor_entity: "sensor.brau_temp",
      heater_entity: "switch.brau_heizung",
      hysteresis: 1.0,
      safety_offset: 5,
      // Die Render-Assertions sind deutsch; der App-Default ist Englisch.
      language: "de",
      unit: "°C",
      ...settings,
    },
  };
}

/**
 * Gemocktes hass-Objekt.
 * @param {Object} [opts]
 * @param {Object} [opts.states]   Zusätzliche/überschreibende States.
 * @param {boolean} [opts.isAdmin] `hass.user.is_admin`.
 * @param {Function} [opts.callWS] Eigener `callWS`-Spy.
 * @param {Object} [opts.userData] Werte für `frontend/get_user_data`.
 * @param {Function} [opts.subscribe] Eigene `subscribeMessage`-Implementierung.
 */
function makeHass(opts = {}) {
  const unsub = vi.fn();
  const userData = opts.userData ?? {};
  const connection = {
    subscribeMessage: vi.fn(opts.subscribe ?? (() => Promise.resolve(unsub))),
    sendMessagePromise: vi.fn((msg) => {
      if (msg.type === "frontend/get_user_data") {
        return Promise.resolve({ value: userData[msg.key] ?? null });
      }
      return Promise.resolve(null);
    }),
  };
  return {
    states: {
      "sensor.brau_temp": { state: "65.0", attributes: { unit_of_measurement: "°C" } },
      "switch.brau_heizung": { state: "off" },
      ...(opts.states ?? {}),
    },
    callService: vi.fn(() => Promise.resolve()),
    callWS: opts.callWS ?? vi.fn(() => Promise.resolve(null)),
    user: { is_admin: opts.isAdmin ?? true },
    connection,
    _unsub: unsub,
  };
}

/**
 * Erzeugt eine Card mit Snapshot (ohne Abo). `snapshot === null` simuliert
 * „nicht geladen".
 */
function makeCard(snapshot = {}, hassOpts = {}) {
  const card = document.createElement("brausteuerung-card");
  card.hass = makeHass(hassOpts);
  card._snapshot = snapshot === null ? null : makeSnapshot(snapshot);
  return card;
}

/** Stubbt `_q` mit festen Formularwerten (kein Shadow-DOM in der Testumgebung). */
function stubInputs(card, values) {
  const els = Object.fromEntries(
    Object.entries(values).map(([sel, value]) => [sel, { value }])
  );
  card._q = (sel) => els[sel] ?? null;
  return els;
}

/** WS-Fehler wie von home-assistant-js-websocket. */
function wsError(code) {
  return Object.assign(new Error(code), { code });
}

/** Lässt ausstehende Promise-Ketten (echte Timer) abarbeiten. */
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

const render = (card) => card.render()._html;

// =============================================================================
// Render
// =============================================================================
describe("render() — Status, Temperatur, Schwelle, Hysterese", () => {
  it("zeigt Status, Ist-Temperatur inkl. Einheit und Hysterese", () => {
    const out = render(makeCard({ settings: { hysteresis: 1.5 } }));
    expect(out).toContain("bereit");
    expect(out).toContain("65.0 °C");
    expect(out).toContain("🌡 Hysterese: 1.5 °C");
  });

  it("zeigt im Ruhezustand den Sicherheits-Offset als 𝚫t", () => {
    const out = render(makeCard({ safety_threshold: null, settings: { safety_offset: 5 } }));
    expect(out).toContain("🛡 Sicherheitsabschaltung bei 𝚫t 5 °C");
  });

  it("zeigt während des Brauens die Schwelle aus dem Snapshot mit 𝚫t", () => {
    const out = render(makeCard({ status: "running", safety_threshold: 72.5 }));
    expect(out).toContain("𝚫t 72.5 °C");
    expect(out).toContain("läuft");
  });

  it("zeigt '—', wenn weder Schwelle noch Offset vorliegen", () => {
    const out = render(makeCard({ settings: { safety_offset: null } }));
    expect(out).toContain("Sicherheitsabschaltung bei —");
  });

  it("zeigt 'Kein Sensor gesetzt' ohne konfigurierten Sensor", () => {
    const out = render(makeCard({ settings: { sensor_entity: null } }));
    expect(out).toContain("Kein Sensor gesetzt");
  });

  it("zeigt 'Kein gültiger Sensorwert' bei unavailable", () => {
    const out = render(
      makeCard({}, { states: { "sensor.brau_temp": { state: "unavailable" } } })
    );
    expect(out).toContain("Kein gültiger Sensorwert");
  });
});

describe("render() — Heizung und Countdown", () => {
  it("zeigt 'AN' mit Flamme bei eingeschalteter Heizung", () => {
    const out = render(makeCard({}, { states: { "switch.brau_heizung": { state: "on" } } }));
    expect(out).toContain("🔥");
    expect(out).toContain("AN");
  });

  it("zeigt 'AUS' ohne Flamme bei ausgeschalteter Heizung", () => {
    const out = render(makeCard());
    expect(out).toContain("AUS");
    expect(out).not.toContain("🔥");
  });

  it("zählt die Haltezeit aus hold_ends_at herunter", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-06T12:00:00Z"));
    const card = makeCard({
      status: "running",
      phase: "holding",
      hold_ends_at: "2026-11-06T12:02:05+00:00",
    });
    expect(card._remainingHoldSeconds).toBe(125);
    expect(render(card)).toContain("Verbleibende Haltezeit: 02:05");
  });

  it("zeigt keinen Countdown ohne hold_ends_at", () => {
    const card = makeCard({ status: "running", phase: "heating" });
    expect(card._remainingHoldSeconds).toBeNull();
    expect(render(card)).not.toContain("Verbleibende Haltezeit");
  });
});

describe("render() — Rezeptname, Buttons, Platzhalter", () => {
  it("zeigt den Rezeptnamen in der Kopfzeile", () => {
    const out = render(makeCard({ recipe_name: "Helles" }));
    expect(out).toContain('class="recipe-name"');
    expect(out).toContain("Helles");
  });

  it("zeigt ohne Namen den Platzhalter (de/en)", () => {
    expect(render(makeCard({ recipe_name: "" }))).toContain("Neues Rezept");
    expect(render(makeCard({ recipe_name: "", settings: { language: "en" } }))).toContain(
      "New recipe"
    );
  });

  it("zeigt im Bibliotheks-Panel '💾 Speichern' bzw. '💾 Save'", () => {
    const de = makeCard();
    de._showLibrary = true;
    expect(render(de)).toContain("💾 Speichern");
    const en = makeCard({ settings: { language: "en" } });
    en._showLibrary = true;
    expect(render(en)).toContain("💾 Save");
  });

  it("listet die Bibliothek und deaktiviert 'Laden' während running", () => {
    const card = makeCard({ status: "running", library: [{ name: "Helles", steps: [] }] });
    card._showLibrary = true;
    const out = render(card);
    expect(out).toContain("Rezepte verwalten");
    expect(out).toContain("Helles");
    expect(out).toContain("?disabled=true");
  });

  it("aktiviert Start bei Rezept und gültigem Sensor, deaktiviert ihn bei leerem Rezept", () => {
    const ready = render(makeCard());
    expect(ready).toContain("Brauprozess starten");
    expect(ready).not.toContain("Start benötigt");
    expect(render(makeCard({ recipe: [] }))).toContain("Start benötigt mindestens eine Rast");
  });

  it("zeigt 'Nächste Rast' und 'Stopp' während running", () => {
    const out = render(makeCard({ status: "running" }));
    expect(out).toContain("Nächste Rast");
    expect(out).toContain("Stopp");
  });

  it("bietet sensor.*- und switch.*-Entitäten als datalist an", () => {
    const card = makeCard({}, { states: { "sensor.kessel": { state: "1" }, "light.x": { state: "on" } } });
    card._showSettings = true;
    const out = render(card);
    expect(out).toContain('<option value=sensor.kessel>');
    expect(out).toContain('<option value=switch.brau_heizung>');
    expect(out).not.toContain("light.x");
  });

  it("zeigt ohne Snapshot nur den Platzhalter err_not_loaded", () => {
    const card = makeCard(null);
    const out = render(card);
    expect(out).toContain('class="placeholder"');
    expect(out).toContain("integration is not loaded");
    expect(out).not.toContain("Start");
    expect(out).not.toContain('class="graph"');
    expect(out).not.toContain('class="add-form"');
  });

  it("zeigt bei abweichender Server-Version err_version_mismatch im Fehlerbereich", () => {
    const card = makeCard({}, { isAdmin: false });
    card._onSnapshot(makeSnapshot({ version: "3.0.0" }));
    const out = render(card);
    expect(out).toContain('class="error"');
    expect(out).toContain("3.0.0");
    expect(out).toContain("3.0.1");
    // Gleiche Version → kein Hinweis.
    card._onSnapshot(makeSnapshot());
    expect(render(card)).not.toContain('class="error"');
  });
});

// =============================================================================
// Aktionen → WebSocket-Befehle
// =============================================================================
describe("Aktionen — Rezept", () => {
  it("_addStep sendet rest/add und leert die Felder nach Erfolg", async () => {
    const card = makeCard({ recipe: [] });
    const els = stubInputs(card, { "#new-name": "Maischen", "#new-temp": "67", "#new-dur": "60" });
    await card._addStep();
    expect(card.hass.callWS).toHaveBeenCalledWith({
      type: "brausteuerung/rest/add",
      name: "Maischen",
      temperature: 67,
      duration: 60,
    });
    expect(els["#new-name"].value).toBe("");
    expect(els["#new-temp"].value).toBe("");
  });

  it("_addStep sendet bei ungültiger Eingabe nichts und meldet einen Fehler", async () => {
    const card = makeCard();
    stubInputs(card, { "#new-name": "", "#new-temp": "200", "#new-dur": "60" });
    await card._addStep();
    expect(card.hass.callWS).not.toHaveBeenCalled();
    expect(card._errorMessage).toContain("Ungültige Eingabe");
  });

  it("_addStep behält die Felder bei Serverfehler und zeigt die Meldung", async () => {
    const card = makeCard({}, { callWS: vi.fn(() => Promise.reject(wsError("too_many_rests"))) });
    const els = stubInputs(card, { "#new-name": "x", "#new-temp": "50", "#new-dur": "5" });
    await card._addStep();
    expect(els["#new-temp"].value).toBe("50");
    expect(card._errorMessage).toBe("Zu viele Rasten (max. 30).");
  });

  it("_saveEdit sendet rest/update und beendet den Edit-Modus erst nach Erfolg", async () => {
    const callWS = vi.fn(() => Promise.reject(wsError("recipe_locked")));
    const card = makeCard({}, { callWS });
    card._editIndex = 0;
    stubInputs(card, { "#edit-name": "Eiweiß", "#edit-temp": "55", "#edit-dur": "15" });
    await card._saveEdit(0);
    expect(callWS).toHaveBeenCalledWith({
      type: "brausteuerung/rest/update",
      index: 0,
      name: "Eiweiß",
      temperature: 55,
      duration: 15,
    });
    expect(card._editIndex).toBe(0);
    expect(card._errorMessage).toContain("nicht geändert werden");

    callWS.mockImplementation(() => Promise.resolve(null));
    await card._saveEdit(0);
    expect(card._editIndex).toBe(-1);
  });

  it("_remove, _clear und _moveStep senden die passenden Befehle", async () => {
    const card = makeCard({
      recipe: [
        { name: "a", temperature: 50, duration: 10 },
        { name: "b", temperature: 60, duration: 10 },
      ],
    });
    await card._remove(1);
    await card._clear();
    await card._moveStep(0, 1);
    expect(card.hass.callWS.mock.calls.map((c) => c[0])).toEqual([
      { type: "brausteuerung/rest/delete", index: 1 },
      { type: "brausteuerung/recipe/clear" },
      { type: "brausteuerung/rest/move", index: 0, direction: 1 },
    ]);
  });

  it("_moveStep sendet nichts an den Listengrenzen oder während running", async () => {
    const card = makeCard();
    await card._moveStep(0, -1);
    await card._moveStep(0, 1);
    expect(card.hass.callWS).not.toHaveBeenCalled();
    const running = makeCard({
      status: "running",
      recipe: [
        { name: "a", temperature: 50, duration: 10 },
        { name: "b", temperature: 60, duration: 10 },
      ],
    });
    await running._moveStep(0, 1);
    expect(running.hass.callWS).not.toHaveBeenCalled();
  });
});

describe("Aktionen — Steuerung", () => {
  it("_start sendet control/start", async () => {
    const card = makeCard();
    await card._start();
    expect(card.hass.callWS).toHaveBeenCalledWith({ type: "brausteuerung/control/start" });
  });

  it("_start sendet nichts bei leerem Rezept oder ungültigem Sensor", async () => {
    const empty = makeCard({ recipe: [] });
    await empty._start();
    expect(empty.hass.callWS).not.toHaveBeenCalled();
    const invalid = makeCard({}, { states: { "sensor.brau_temp": { state: "unknown" } } });
    await invalid._start();
    expect(invalid.hass.callWS).not.toHaveBeenCalled();
  });

  it("_stop und _nextStep senden control/stop bzw. control/next_rest", async () => {
    const card = makeCard({ status: "running" });
    await card._stop();
    await card._nextStep();
    expect(card.hass.callWS.mock.calls.map((c) => c[0].type)).toEqual([
      "brausteuerung/control/stop",
      "brausteuerung/control/next_rest",
    ]);
  });

  it("_toggleHeater schaltet weiter direkt über den switch-Service", () => {
    const card = makeCard();
    card._toggleHeater();
    expect(card.hass.callService).toHaveBeenCalledWith("switch", "turn_on", {
      entity_id: "switch.brau_heizung",
    });
    expect(card.hass.callWS).not.toHaveBeenCalled();
  });

  it("bildet Fehlercodes auf Übersetzungen ab", async () => {
    const card = makeCard();
    card.hass.callWS = vi.fn(() => Promise.reject(wsError("legacy_automations_active")));
    await card._start();
    expect(card._errorMessage).toContain("Alte Brausteuerung-Automationen");
    card.hass.callWS = vi.fn(() => Promise.reject(wsError("something_new")));
    await card._start();
    expect(card._errorMessage).toBe("Aktion fehlgeschlagen (something_new).");
    card.hass.callWS = vi.fn(() => Promise.reject(wsError("invalid_format")));
    await card._start();
    expect(card._errorMessage).toBe("Aktion fehlgeschlagen (invalid_format).");
  });
});

describe("Aktionen — Bibliothek", () => {
  it("_loadRecipeFromLibrary sendet library/load, nicht während running", async () => {
    const card = makeCard();
    expect(await card._loadRecipeFromLibrary("Helles")).toBe(true);
    expect(card.hass.callWS).toHaveBeenCalledWith({
      type: "brausteuerung/library/load",
      name: "Helles",
    });
    const running = makeCard({ status: "running" });
    expect(await running._loadRecipeFromLibrary("Helles")).toBe(false);
    expect(running.hass.callWS).not.toHaveBeenCalled();
  });

  it("_deleteRecipeFromLibrary sendet library/delete", async () => {
    const card = makeCard();
    await card._deleteRecipeFromLibrary("Helles");
    expect(card.hass.callWS).toHaveBeenCalledWith({
      type: "brausteuerung/library/delete",
      name: "Helles",
    });
  });

  it("_onSaveRecipeClick speichert unter neuem Namen und leert das Feld", async () => {
    const card = makeCard();
    const els = stubInputs(card, { "#library-name": "  Helles " });
    await card._onSaveRecipeClick();
    expect(card.hass.callWS).toHaveBeenCalledWith({
      type: "brausteuerung/library/save",
      name: "Helles",
    });
    expect(els["#library-name"].value).toBe("");
  });

  it("fragt bei name_exists nach und überschreibt mit overwrite:true", async () => {
    const callWS = vi
      .fn()
      .mockImplementationOnce(() => Promise.reject(wsError("name_exists")))
      .mockImplementationOnce(() => Promise.resolve(null));
    const card = makeCard({}, { callWS });
    stubInputs(card, { "#library-name": "Helles" });
    const confirm = vi.spyOn(window, "confirm").mockImplementation(() => true);
    await card._onSaveRecipeClick();
    expect(confirm).toHaveBeenCalledWith('Rezept "Helles" existiert bereits. Überschreiben?');
    expect(callWS.mock.calls.map((c) => c[0])).toEqual([
      { type: "brausteuerung/library/save", name: "Helles" },
      { type: "brausteuerung/library/save", name: "Helles", overwrite: true },
    ]);
  });

  it("überschreibt nicht, wenn die Bestätigung abgelehnt wird", async () => {
    const callWS = vi.fn(() => Promise.reject(wsError("name_exists")));
    const card = makeCard({}, { callWS });
    stubInputs(card, { "#library-name": "Helles" });
    vi.spyOn(window, "confirm").mockImplementation(() => false);
    await card._onSaveRecipeClick();
    expect(callWS).toHaveBeenCalledTimes(1);
  });

  it("lehnt einen leeren Namen ab, ohne zu senden", async () => {
    const card = makeCard();
    stubInputs(card, { "#library-name": "   " });
    await card._onSaveRecipeClick();
    expect(card.hass.callWS).not.toHaveBeenCalled();
    expect(card._errorMessage).toContain("Namen");
  });

  it("zeigt bei library_full die passende Meldung", async () => {
    const card = makeCard({}, { callWS: vi.fn(() => Promise.reject(wsError("library_full"))) });
    stubInputs(card, { "#library-name": "Neu" });
    await card._onSaveRecipeClick();
    expect(card._errorMessage).toContain("Bibliothek ist voll");
  });
});

describe("Aktionen — Sprache, Einheit, Einstellungen", () => {
  it("_setLanguage/_setUnit senden display/update (normalisiert)", async () => {
    const card = makeCard();
    await card._setLanguage("de");
    await card._setLanguage("fr");
    await card._setUnit("°F");
    await card._setUnit("K");
    expect(card.hass.callWS.mock.calls.map((c) => c[0])).toEqual([
      { type: "brausteuerung/display/update", language: "de" },
      { type: "brausteuerung/display/update", language: "en" },
      { type: "brausteuerung/display/update", unit: "°F" },
      { type: "brausteuerung/display/update", unit: "°C" },
    ]);
  });

  it("_saveSettings sendet nur geänderte Felder und schließt danach", async () => {
    const card = makeCard();
    card._showSettings = true;
    stubInputs(card, {
      "#settings-sensor": "sensor.brau_temp",
      "#settings-heater": "switch.neue_heizung",
      "#settings-hysteresis": "1",
    });
    await card._saveSettings();
    expect(card.hass.callWS).toHaveBeenCalledTimes(1);
    expect(card.hass.callWS).toHaveBeenCalledWith({
      type: "brausteuerung/settings/update",
      heater_entity: "switch.neue_heizung",
    });
    expect(card._showSettings).toBe(false);
  });

  it("_saveSettings ohne Änderung sendet nichts und schließt das Panel", async () => {
    const card = makeCard();
    card._showSettings = true;
    stubInputs(card, {
      "#settings-sensor": "sensor.brau_temp",
      "#settings-heater": "switch.brau_heizung",
      "#settings-hysteresis": "1.0",
    });
    await card._saveSettings();
    expect(card.hass.callWS).not.toHaveBeenCalled();
    expect(card._showSettings).toBe(false);
  });

  it("_saveSettings lehnt ungültige Hysterese ab und lässt das Panel offen", async () => {
    const card = makeCard();
    card._showSettings = true;
    stubInputs(card, {
      "#settings-sensor": "sensor.brau_temp",
      "#settings-heater": "switch.brau_heizung",
      "#settings-hysteresis": "7",
    });
    await card._saveSettings();
    expect(card.hass.callWS).not.toHaveBeenCalled();
    expect(card._showSettings).toBe(true);
    expect(card._errorMessage).toContain("Ungültige Hysterese");
  });

  it("unauthorized → Admin-Meldung, Panel bleibt offen", async () => {
    const card = makeCard({}, { callWS: vi.fn(() => Promise.reject(wsError("unauthorized"))) });
    card._showSettings = true;
    stubInputs(card, {
      "#settings-sensor": "sensor.brau_temp",
      "#settings-heater": "switch.brau_heizung",
      "#settings-hysteresis": "2.5",
    });
    await card._saveSettings();
    expect(card.hass.callWS).toHaveBeenCalledWith({
      type: "brausteuerung/settings/update",
      hysteresis: 2.5,
    });
    expect(card._errorMessage).toBe("Diese Einstellung kann nur ein Administrator ändern.");
    expect(card._showSettings).toBe(true);
  });

  it("invalid_format bei geänderter Entity → Entity-Meldung, Panel bleibt offen", async () => {
    const card = makeCard({}, { callWS: vi.fn(() => Promise.reject(wsError("invalid_format"))) });
    card._showSettings = true;
    stubInputs(card, {
      "#settings-sensor": "Sensor.Tippfehler",
      "#settings-heater": "switch.brau_heizung",
      "#settings-hysteresis": "1",
    });
    await card._saveSettings();
    expect(card.hass.callWS).toHaveBeenCalledWith({
      type: "brausteuerung/settings/update",
      sensor_entity: "Sensor.Tippfehler",
    });
    expect(card._errorMessage).toContain("Ungültige Entity-ID");
    expect(card._showSettings).toBe(true);
  });
});

// =============================================================================
// Graph
// =============================================================================
describe("Temperaturverlauf-Graph", () => {
  it("nutzt den Sollwert-Sensor aus snapshot.entities.setpoint", () => {
    const config = makeCard()._graphCardConfig();
    expect(config.type).toBe("history-graph");
    expect(config.entities.map((e) => e.entity)).toEqual(["sensor.brau_temp", SETPOINT_ENTITY]);
  });

  it("begrenzt das Fenster bei running auf die Zeit seit started_at", () => {
    const start = new Date(Date.now() - 30 * 60000).toISOString();
    const card = makeCard({ status: "running", started_at: start });
    card._graphHours = 2;
    expect(card._effectiveGraphHours()).toBeGreaterThan(0.4);
    expect(card._effectiveGraphHours()).toBeLessThan(0.6);
    const old = makeCard({
      status: "running",
      started_at: new Date(Date.now() - 5 * 3600000).toISOString(),
    });
    old._graphHours = 2;
    expect(old._effectiveGraphHours()).toBe(2);
  });

  it("nutzt ohne laufenden Brauvorgang die gewählte Dauer und klemmt 1..4", () => {
    const card = makeCard();
    card._setGraphHours(3);
    expect(card._effectiveGraphHours()).toBe(3);
    card._setGraphHours(99);
    expect(card._graphHours).toBe(4);
    card._setGraphHours(0);
    expect(card._graphHours).toBe(1);
  });

  it("rendert Titel, Dauer-Auswahl und Ladehinweis bzw. Sensor-Aufforderung", () => {
    const out = render(makeCard());
    expect(out).toContain("Temperaturverlauf");
    expect(out).toContain("4 h");
    expect(out).toContain("Lade Temperaturverlauf");
    expect(render(makeCard({ settings: { sensor_entity: "" } }))).toContain(
      "Temperatursensor auswählen"
    );
  });
});

// =============================================================================
// Bibliotheksmigration (Design §10.2)
// =============================================================================
describe("Bibliotheksmigration aus user_data", () => {
  const oldLibrary = JSON.stringify([
    { name: "Helles", steps: [{ name: "Rast 1", temperature: 63, duration: 45 }] },
    { name: 42, steps: [] },
  ]);

  it("importiert als Admin einmalig und setzt danach den Merker", async () => {
    const card = makeCard({}, {
      userData: {
        brausteuerung_recipes: oldLibrary,
        brausteuerung_active_recipe: JSON.stringify({ name: "Helles" }),
      },
    });
    card.hass.callWS = vi.fn(() => Promise.resolve({ imported: 1, skipped: 0 }));
    await card._migrateLibrary();
    expect(card.hass.callWS).toHaveBeenCalledTimes(1);
    expect(card.hass.callWS).toHaveBeenCalledWith({
      type: "brausteuerung/library/import",
      recipes: [{ name: "Helles", steps: [{ name: "Rast 1", temperature: 63, duration: 45 }] }],
      active_recipe_name: "Helles",
    });
    expect(card.hass.connection.sendMessagePromise).toHaveBeenCalledWith({
      type: "frontend/set_user_data",
      key: "brausteuerung_recipes_migrated",
      value: true,
    });
    // Zweiter Aufruf (weiteres Snapshot-Event) importiert nicht erneut.
    await card._migrateLibrary();
    expect(card.hass.callWS).toHaveBeenCalledTimes(1);
  });

  it("wird durch das erste Snapshot-Event angestoßen", async () => {
    const card = makeCard(null, { userData: { brausteuerung_recipes: oldLibrary } });
    card.hass.callWS = vi.fn(() => Promise.resolve({ imported: 1, skipped: 0 }));
    card._onSnapshot(makeSnapshot());
    card._onSnapshot(makeSnapshot());
    await flush();
    await flush();
    expect(card.hass.callWS).toHaveBeenCalledTimes(1);
  });

  it("macht nichts für Nicht-Admins", async () => {
    const card = makeCard({}, { isAdmin: false, userData: { brausteuerung_recipes: oldLibrary } });
    await card._migrateLibrary();
    expect(card.hass.callWS).not.toHaveBeenCalled();
    expect(card.hass.connection.sendMessagePromise).not.toHaveBeenCalled();
  });

  it("überspringt den Import, wenn der Merker gesetzt ist", async () => {
    const card = makeCard({}, {
      userData: { brausteuerung_recipes_migrated: true, brausteuerung_recipes: oldLibrary },
    });
    await card._migrateLibrary();
    expect(card.hass.callWS).not.toHaveBeenCalled();
  });

  it("setzt bei leerer alter Bibliothek nur den Merker", async () => {
    const card = makeCard();
    await card._migrateLibrary();
    expect(card.hass.callWS).not.toHaveBeenCalled();
    expect(card.hass.connection.sendMessagePromise).toHaveBeenCalledWith({
      type: "frontend/set_user_data",
      key: "brausteuerung_recipes_migrated",
      value: true,
    });
  });

  it("meldet übersprungene Einträge im Fehlerbereich", async () => {
    const card = makeCard({}, { userData: { brausteuerung_recipes: oldLibrary } });
    card.hass.callWS = vi.fn(() => Promise.resolve({ imported: 0, skipped: 2 }));
    await card._migrateLibrary();
    expect(card._errorMessage).toBe("2 Rezepte aus der alten Bibliothek konnten nicht übernommen werden.");
  });

  it("setzt den Merker nicht, wenn der Import scheitert", async () => {
    const card = makeCard({}, { userData: { brausteuerung_recipes: oldLibrary } });
    card.hass.callWS = vi.fn(() => Promise.reject(wsError("unauthorized")));
    await card._migrateLibrary();
    expect(card.hass.connection.sendMessagePromise).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: "frontend/set_user_data", key: "brausteuerung_recipes_migrated" })
    );
  });
});

// =============================================================================
// Snapshot-Abo (Design §12.1, Finding 17)
// =============================================================================
describe("Snapshot-Abo", () => {
  function connectedCard(hassOpts = {}) {
    const card = document.createElement("brausteuerung-card");
    card.hass = makeHass({ isAdmin: false, ...hassOpts });
    card.connectedCallback();
    return card;
  }

  it("abonniert brausteuerung/subscribe und übernimmt den Snapshot", () => {
    const card = connectedCard();
    const sub = card.hass.connection.subscribeMessage;
    expect(sub).toHaveBeenCalledTimes(1);
    const [cb, msg] = sub.mock.calls[0];
    expect(msg).toEqual({ type: "brausteuerung/subscribe" });
    cb(makeSnapshot({ recipe_name: "Helles" }));
    expect(card._activeRecipeName).toBe("Helles");
    expect(card._notLoaded).toBe(false);
    // Weitere hass-Updates abonnieren nicht doppelt.
    card.updated(new Map([["hass", undefined]]));
    expect(sub).toHaveBeenCalledTimes(1);
    card.disconnectedCallback();
  });

  it("{loaded:false} → unsub, Platzhalter und genau ein Retry nach 10 s", async () => {
    vi.useFakeTimers();
    const card = connectedCard();
    const sub = card.hass.connection.subscribeMessage;
    const [cb] = sub.mock.calls[0];
    cb(makeSnapshot());
    cb({ loaded: false });
    cb({ loaded: false });
    expect(card._snapshot).toBeNull();
    expect(card._notLoaded).toBe(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(card.hass._unsub).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(9999);
    expect(sub).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(sub).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(30000);
    expect(sub).toHaveBeenCalledTimes(2);
    card.disconnectedCallback();
  });

  it("abgelehntes Abo → genau ein Retry nach 10 s", async () => {
    vi.useFakeTimers();
    const card = connectedCard({ subscribe: () => Promise.reject(wsError("unknown_command")) });
    const sub = card.hass.connection.subscribeMessage;
    await vi.advanceTimersByTimeAsync(0);
    expect(card._notLoaded).toBe(true);
    await vi.advanceTimersByTimeAsync(10000);
    expect(sub).toHaveBeenCalledTimes(2);
    card.disconnectedCallback();
  });

  it("disconnectedCallback → unsub, kein Retry; connectedCallback abonniert neu", async () => {
    vi.useFakeTimers();
    const card = connectedCard();
    const sub = card.hass.connection.subscribeMessage;
    card.disconnectedCallback();
    await vi.advanceTimersByTimeAsync(0);
    expect(card.hass._unsub).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(20000);
    expect(sub).toHaveBeenCalledTimes(1);
    card.connectedCallback();
    expect(sub).toHaveBeenCalledTimes(2);
    card.disconnectedCallback();
  });

  it("verwirft einen geplanten Retry beim Aushängen", async () => {
    vi.useFakeTimers();
    const card = connectedCard();
    const sub = card.hass.connection.subscribeMessage;
    sub.mock.calls[0][0]({ loaded: false });
    card.disconnectedCallback();
    await vi.advanceTimersByTimeAsync(20000);
    expect(sub).toHaveBeenCalledTimes(1);
  });
});

// =============================================================================
// shouldUpdate (Req 7.1, 7.2)
// =============================================================================
describe("shouldUpdate — Snapshot zählt wie hass", () => {
  it("unterdrückt Snapshot-Updates während der Inline-Bearbeitung", () => {
    const card = makeCard();
    card._editIndex = 0;
    expect(card.shouldUpdate(new Map([["_snapshot", null]]))).toBe(false);
    expect(card.shouldUpdate(new Map([["_editIndex", 0]]))).toBe(true);
  });

  it("lässt Snapshot-Updates bei offenem Bibliotheks-Panel zu, hass-Updates nicht", () => {
    const card = makeCard();
    card._showLibrary = true;
    expect(card.shouldUpdate(new Map([["_snapshot", null]]))).toBe(true);
    expect(card.shouldUpdate(new Map([["hass", null]]))).toBe(false);
  });
});

// =============================================================================
// Editor, Registrierung, Version, Quelltext
// =============================================================================
describe("Editor und Registrierung", () => {
  it("getStubConfig liefert {} und der Editor zeigt nur editor_note", () => {
    expect(CardClass.getStubConfig()).toEqual({});
    const editor = document.createElement("brausteuerung-card-editor");
    editor.hass = { language: "de" };
    editor.setConfig({ sensor_entity: "sensor.alt" });
    const out = editor.render()._html;
    expect(out).toContain("Geräte & Dienste");
    expect(out).not.toContain("<input");
  });

  it("registriert die Karte mit Version in window.customCards", () => {
    const entry = window.customCards.find((c) => c.type === "brausteuerung-card");
    expect(entry.name).toContain("v3.0.1");
  });

  it("warnt, wenn brausteuerung-card beim Laden schon definiert ist", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.resetModules();
    await import(CARD_PATH);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0][0]).toContain("/local/brausteuerung-card.js");
    expect(customElements.get("brausteuerung-card")).toBe(CardClass);
  });
});

describe("Version und Quelltext (AK 6, AK 30)", () => {
  const source = fs.readFileSync(CARD_FILE, "utf8");

  it("VERSION == manifest.json == package.json", () => {
    const match = source.match(/const VERSION = "([^"]+)"/);
    expect(match).not.toBeNull();
    const manifest = JSON.parse(
      fs.readFileSync(
        path.resolve(process.cwd(), "custom_components/brausteuerung/manifest.json"),
        "utf8"
      )
    );
    const pkg = JSON.parse(fs.readFileSync(path.resolve(process.cwd(), "package.json"), "utf8"));
    expect(match[1]).toBe("3.0.1");
    expect(manifest.version).toBe(match[1]);
    expect(pkg.version).toBe(match[1]);
  });

  it("lädt das Logikmodul über import.meta.url ohne festes ?v=", () => {
    expect(source).toContain("import.meta.url");
    expect(source).toContain("await import(/* @vite-ignore */ LOGIC_URL)");
    expect(source).not.toMatch(/brausteuerung-logic\.js\?v=/);
    expect(source).not.toMatch(/^import .* from "\.\/brausteuerung-logic\.js/m);
    expect(source).toContain("const LOGIC_URL = logicUrlFor(import.meta.url);");
  });

  it("logicUrlFor übernimmt die ?v= der Karten-URL (AK 7)", () => {
    const { logicUrlFor } = cardModule;
    const logic = new URL(
      logicUrlFor("http://ha.local:8123/brausteuerung_frontend/brausteuerung-card.js?v=3.0.1")
    );
    expect(`${logic.pathname}${logic.search}`).toBe(
      "/brausteuerung_frontend/brausteuerung-logic.js?v=3.0.1"
    );
    expect(logic.origin).toBe("http://ha.local:8123");
    // Ohne Query an der Karte auch keine Query am Logikmodul.
    expect(logicUrlFor("http://ha.local/brausteuerung_frontend/brausteuerung-card.js")).toBe(
      "http://ha.local/brausteuerung_frontend/brausteuerung-logic.js"
    );
  });

  it("enthält keine Legacy-Referenzen und keine 255-Logik", () => {
    expect(source).not.toContain("input_text.brau_");
    expect(source).not.toContain("automation.brausteuerung_");
    expect(source).not.toContain("255");
  });
});
