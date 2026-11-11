/**
 * brausteuerung-logic.js
 *
 * Reines, Home-Assistant-freies Hilfsmodul der Brausteuerung-Karte.
 *
 * Seit v3.0.0 liegt die gesamte Regelungs-, Sicherheits-, Rezept- und
 * Bibliothekslogik in der Python-Integration (`logic.py`, hypothesis-getestet).
 * Dieses Modul enthält nur noch, was die Karte für Anzeige und sofortiges
 * Eingabe-Feedback braucht: Eingabeprüfungen, Anzeigeprädikate, das robuste
 * Parsen der alten Bibliothek (einmalige Migration), die Graph-Helfer und die
 * Übersetzungen. Alle Funktionen sind rein (keine HA-Seiteneffekte, kein DOM)
 * und damit in Vitest direkt importierbar und mit fast-check testbar.
 *
 * @module brausteuerung-logic
 */

// ---------------------------------------------------------------------------
// Konstanten
// ---------------------------------------------------------------------------

/**
 * Standard-Hystereseband in °C (Req 4.3, 4.9).
 * @type {number}
 */
export const HYSTERESIS_BAND = 1.0;

/**
 * Gültiger Wertebereich des konfigurierbaren Hysteresebandes in °C (Req 4.9):
 * größer als 0 °C bis maximal 5 °C.
 * @type {number}
 */
export const MIN_HYSTERESIS = 0;
/** @type {number} */
export const MAX_HYSTERESIS = 5;

/**
 * Untere und obere Grenze einer gültigen Solltemperatur in °C (Req 2.2).
 * @type {number}
 */
export const MIN_TEMPERATURE = 0;
/** @type {number} */
export const MAX_TEMPERATURE = 100;

/**
 * Mögliche Betriebszustände der Brausteuerung.
 * @readonly
 * @enum {string}
 */
export const Status = Object.freeze({
  IDLE: 'idle',
  RUNNING: 'running',
  PAUSED: 'paused',
  DONE: 'done',
});

// ---------------------------------------------------------------------------
// Typdefinitionen
// ---------------------------------------------------------------------------

/**
 * Eine Raststufe eines Braurezepts.
 * @typedef {Object} Raststufe
 * @property {string} name        Anzeigename; leer ⇒ Standardname (vergibt der Server).
 * @property {number} temperature Solltemperatur in °C (0 ≤ t ≤ 100).
 * @property {number} duration    Haltezeit in Minuten, ganzzahlig > 0.
 */

/**
 * Ein Braurezept als geordnete Liste von Raststufen.
 * @typedef {Raststufe[]} Braurezept
 */

// ---------------------------------------------------------------------------
// Eingabeprüfungen (sofortiges Feedback in der Karte, F26)
// ---------------------------------------------------------------------------

/**
 * Prüft, ob eine Raststufe gültige Werte hat (Req 2.2, 2.3, 2.4).
 *
 * Gültig genau dann, wenn `temp` eine Zahl mit 0 ≤ temp ≤ 100 ist UND
 * `dur` eine Ganzzahl > 0 ist. Der Name wird nicht auf Gültigkeit geprüft.
 * Die verbindliche Prüfung macht der Server; diese Funktion dient nur dem
 * sofortigen Feedback.
 *
 * @param {string} name Name der Raststufe (für Validität irrelevant).
 * @param {number} temp Solltemperatur in °C.
 * @param {number} dur  Haltezeit in Minuten.
 * @returns {boolean} `true`, wenn die Raststufe gültig ist.
 */
export function isValidRaststufe(name, temp, dur) {
  const tempValid =
    typeof temp === 'number' &&
    Number.isFinite(temp) &&
    temp >= MIN_TEMPERATURE &&
    temp <= MAX_TEMPERATURE;
  const durValid = Number.isInteger(dur) && dur > 0;
  return tempValid && durValid;
}

/**
 * Prüft, ob ein Eingabewert ein gültiges Hystereseband ist (Req 4.9).
 *
 * Gültig genau dann, wenn `value` eine endliche Zahl mit `0 < value <= 5` ist.
 *
 * @param {number} value Zu prüfendes Hystereseband in °C.
 * @returns {boolean} `true`, wenn der Wert ein gültiges Hystereseband ist.
 */
export function isValidHysteresis(value) {
  return (
    typeof value === 'number' &&
    Number.isFinite(value) &&
    value > MIN_HYSTERESIS &&
    value <= MAX_HYSTERESIS
  );
}

// ---------------------------------------------------------------------------
// Zustands- und Eingabeprädikate
// ---------------------------------------------------------------------------

/**
 * Prüft, ob ein Sensor-Rohwert gültig ist (Req 1.3).
 *
 * `true` genau dann, wenn der Wert in eine endliche Zahl geparst werden kann;
 * `false` für `unknown`, `unavailable`, leere oder nicht-numerische Werte.
 *
 * @param {string|number|null|undefined} rawValue Sensor-Rohwert.
 * @returns {boolean} `true`, wenn der Wert eine endliche Zahl ist.
 */
export function isSensorValid(rawValue) {
  if (rawValue === null || rawValue === undefined) {
    return false;
  }
  if (typeof rawValue === 'number') {
    return Number.isFinite(rawValue);
  }
  if (typeof rawValue === 'string') {
    const trimmed = rawValue.trim();
    if (trimmed === '') {
      return false;
    }
    const lower = trimmed.toLowerCase();
    if (lower === 'unknown' || lower === 'unavailable') {
      return false;
    }
    return Number.isFinite(Number(trimmed));
  }
  return false;
}

/**
 * Prüft, ob der Brauprozess gestartet werden darf (Req 8.1, 8.2).
 *
 * `true` genau dann, wenn das Rezept mindestens eine Raststufe enthält UND
 * ein gültiger Sensorwert vorliegt.
 *
 * @param {Braurezept} recipe    Aktuelles Rezept.
 * @param {boolean} sensorValid  Ob ein gültiger Sensorwert vorliegt.
 * @returns {boolean} `true`, wenn die Start-Aktion verfügbar ist.
 */
export function canStart(recipe, sensorValid) {
  return Array.isArray(recipe) && recipe.length >= 1 && sensorValid === true;
}

/**
 * Render-Stabilitätsentscheidung für `shouldUpdate` der Card (Req 7.1, 7.2).
 *
 * Ist das Settings-Panel offen (`showSettings`) ODER wird eine Rast editiert
 * (`editIndex >= 0`), wird nur dann neu gerendert, wenn sich `_showSettings`,
 * `_editIndex` oder `_localRecipe` geändert haben — reine `hass`-Änderungen
 * lösen kein Re-Render aus. Andernfalls wird immer neu gerendert.
 *
 * @param {boolean} showSettings        Ob das Settings-Panel offen ist.
 * @param {number} editIndex            Index der editierten Rast (-1 = keine).
 * @param {Set<string>|Map<string, *>} changedProps Geänderte Properties (LitElement).
 * @returns {boolean} `true`, wenn ein Re-Render erfolgen soll.
 */
export function shouldUpdateDecision(showSettings, editIndex, changedProps) {
  if (showSettings || editIndex >= 0) {
    return (
      changedProps.has('_showSettings') ||
      changedProps.has('_editIndex') ||
      changedProps.has('_localRecipe')
    );
  }
  return true;
}

// ---------------------------------------------------------------------------
// Rezeptname und alte Bibliothek (Req 12, 16)
// ---------------------------------------------------------------------------

/**
 * Normalisiert einen Rezeptnamen (trimmt führende/abschließende Whitespaces).
 * @param {string} name Roh-Name.
 * @returns {string} Getrimmter Name (leerer String bei nicht-String/leer).
 */
function normalizeRecipeName(name) {
  return typeof name === 'string' ? name.trim() : '';
}

/**
 * Liefert den anzuzeigenden Namen des aktiven Rezepts (Req 16.1, 16.2).
 *
 * @param {string} name         Gespeicherter Name des aktiven Rezepts.
 * @param {string} placeholder  Platzhalter, wenn kein Name gesetzt ist.
 * @returns {string} Anzuzeigender Name.
 */
export function displayRecipeName(name, placeholder = 'Neues Rezept') {
  const trimmed = normalizeRecipeName(name);
  return trimmed !== '' ? trimmed : placeholder;
}

/**
 * Parst den Rohwert der alten Bibliothek aus dem HA-Benutzerspeicher robust
 * (Req 12.8). Wird nur noch für die einmalige Migration nach v3.0.0 genutzt;
 * die eigentliche Prüfung jedes Eintrags macht der Server (`library/import`).
 *
 * Akzeptiert sowohl bereits geparste Arrays als auch JSON-Strings. Bei
 * ungültigem JSON, fehlendem Wert oder Nicht-Array wird eine leere Bibliothek
 * `[]` geliefert. Einträge ohne gültige Struktur (`name`-String und
 * `steps`-Array) werden verworfen.
 *
 * @param {unknown} raw Rohwert (Array, JSON-String, null, …).
 * @returns {Array} Geparste, bereinigte Bibliothek.
 */
export function parseLibrary(raw) {
  let data = raw;
  if (typeof raw === 'string') {
    try {
      data = JSON.parse(raw);
    } catch {
      return [];
    }
  }
  if (!Array.isArray(data)) {
    return [];
  }
  return data
    .filter((r) => r && typeof r.name === 'string' && Array.isArray(r.steps))
    .map((r) => ({ name: r.name, steps: r.steps }));
}

// ---------------------------------------------------------------------------
// Temperaturverlauf-Graph (Req 13) — reine, HA-/DOM-freie Hilfsfunktionen
// ---------------------------------------------------------------------------
//
// Der Graph wird in der Card aus selbst aufgezeichneten Messpunkten gerendert.
// Ein Messpunkt (`GraphSample`) hat die Form:
//   { t: number (ms seit Epoch), temp: number (°C), soll: number|null (°C) }
// Die folgenden Funktionen sind rein und immutabel und damit property-testbar.

/**
 * Standard-/Grenzwerte für die wählbare Anzeigedauer des Graphen in Stunden
 * (Req 13.3): wählbar 1–4 h, Default 2 h.
 * @type {number}
 */
export const MIN_GRAPH_HOURS = 1;
/** @type {number} */
export const MAX_GRAPH_HOURS = 4;
/** @type {number} */
export const DEFAULT_GRAPH_HOURS = 2;

/**
 * Begrenzt einen (möglicherweise ungültigen) Stundenwert auf den gültigen
 * Bereich 1–4 (ganzzahlig). Ungültige/fehlende Werte ⇒ Default 2 (Req 13.3).
 *
 * @param {number|string|null|undefined} value Roh-Stundenwert.
 * @returns {number} Ganzzahliger Stundenwert im Bereich {@link MIN_GRAPH_HOURS}..{@link MAX_GRAPH_HOURS}.
 */
export function clampGraphHours(value) {
  if (value === null || value === undefined) {
    return DEFAULT_GRAPH_HOURS;
  }
  if (typeof value === 'string' && value.trim() === '') {
    return DEFAULT_GRAPH_HOURS;
  }
  const num = typeof value === 'string' ? Number(value) : value;
  if (typeof num !== 'number' || !Number.isFinite(num)) {
    return DEFAULT_GRAPH_HOURS;
  }
  const rounded = Math.round(num);
  if (rounded < MIN_GRAPH_HOURS) return MIN_GRAPH_HOURS;
  if (rounded > MAX_GRAPH_HOURS) return MAX_GRAPH_HOURS;
  return rounded;
}

/**
 * Liefert das dauerabhängige Aufzeichnungsintervall in Millisekunden (Req 13.8).
 *
 * Das Intervall skaliert mit der gewählten Anzeigedauer, sodass die Anzahl der
 * gehaltenen Messpunkte über alle Dauern ähnlich bleibt: 15 s je Stunde
 * Anzeigedauer (1 h → 15 s, 2 h → 30 s, 3 h → 45 s, 4 h → 60 s).
 *
 * @param {number|string} hours Anzeigedauer in Stunden.
 * @returns {number} Aufzeichnungsintervall in Millisekunden.
 */
export function graphSampleIntervalMs(hours) {
  return clampGraphHours(hours) * 15 * 1000;
}

/**
 * Liefert den dauerabhängigen Abstand der Zeitachsen-Beschriftung in Minuten
 * (Req 13.9). Skaliert mit der Anzeigedauer: 15 min je Stunde (1 h → 15 min,
 * 2 h → 30 min, 3 h → 45 min, 4 h → 60 min). So bleibt die Achse über alle
 * Dauern ähnlich dicht beschriftet, der „Maßstab" passt sich aber an.
 *
 * @param {number|string} hours Anzeigedauer in Stunden.
 * @returns {number} Tick-Abstand der Zeitachse in Minuten.
 */
export function graphTimeStepMinutes(hours) {
  return clampGraphHours(hours) * 15;
}

/**
 * Formatiert einen Zeitstempel als absolute Uhrzeit im Format `hh:mm`
 * (lokale Zeit, 24 h) für die Zeitachsen-Beschriftung (Req 13.9).
 *
 * @param {number|Date} ms Zeitstempel in ms seit Epoch oder ein `Date`.
 * @returns {string} Uhrzeit als `hh:mm` (z. B. `"08:05"`).
 */
export function formatClock(ms) {
  const d = ms instanceof Date ? ms : new Date(ms);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

/**
 * Berechnet „runde" Achsenmarken für ein numerisches Intervall nach dem
 * Heckbert-Verfahren (für die Temperaturachse, Req 13.10). Liefert
 * gleichmäßige, gut lesbare Werte (…, 50, 55, 60, …), die das Intervall
 * `[min, max]` umschließen.
 *
 * @param {number} min       Unterer Wert.
 * @param {number} max       Oberer Wert.
 * @param {number} [maxTicks=5] Ungefähre Zielanzahl der Marken.
 * @returns {number[]} Aufsteigende Liste runder Markenwerte.
 */
export function niceTicks(min, max, maxTicks = 5) {
  if (!Number.isFinite(min) || !Number.isFinite(max) || maxTicks < 2) {
    return [];
  }
  let lo = Math.min(min, max);
  let hi = Math.max(min, max);
  if (lo === hi) {
    lo -= 1;
    hi += 1;
  }
  const niceNum = (range, round) => {
    const exp = Math.floor(Math.log10(range));
    const frac = range / Math.pow(10, exp);
    let nf;
    if (round) {
      if (frac < 1.5) nf = 1;
      else if (frac < 3) nf = 2;
      else if (frac < 7) nf = 5;
      else nf = 10;
    } else {
      if (frac <= 1) nf = 1;
      else if (frac <= 2) nf = 2;
      else if (frac <= 5) nf = 5;
      else nf = 10;
    }
    return nf * Math.pow(10, exp);
  };
  const range = niceNum(hi - lo, false);
  const step = niceNum(range / (maxTicks - 1), true);
  if (!Number.isFinite(step) || step <= 0) {
    return [];
  }
  const niceMin = Math.floor(lo / step) * step;
  const niceMax = Math.ceil(hi / step) * step;
  const ticks = [];
  // Dezimalstellen für saubere Rundung (vermeidet 0.30000000000000004);
  // auf den gültigen toFixed-Bereich [0,100] begrenzen.
  const decimals = Math.min(100, Math.max(0, -Math.floor(Math.log10(step))));
  for (let v = niceMin; v <= niceMax + step * 0.5; v += step) {
    ticks.push(Number(v.toFixed(decimals)));
  }
  return ticks;
}

/**
 * Entscheidet, ob aktuell Messpunkte aufgezeichnet werden sollen (Req 13.4, 13.6).
 *
 * Aufgezeichnet wird genau dann, wenn ein Brauprozess läuft (`status === running`)
 * ODER der Benutzer die Anzeige der aktuellen Temperatur auch ohne aktive
 * Brausteuerung aktiviert hat (`showAlways === true`).
 *
 * @param {string} status      Aktueller Betriebsstatus (siehe {@link Status}).
 * @param {boolean} showAlways Ob auch ohne laufenden Prozess aufgezeichnet wird.
 * @returns {boolean} `true`, wenn ein Messpunkt aufgezeichnet werden soll.
 */
export function shouldRecordSample(status, showAlways) {
  return status === Status.RUNNING || showAlways === true;
}

/**
 * Entfernt Messpunkte, die älter als das Zeitfenster (`windowHours` Stunden vor
 * `now`) sind (Req 13.3). Die Reihenfolge der verbleibenden Punkte bleibt
 * erhalten; die Eingabeliste wird nicht mutiert.
 *
 * @param {Array} samples     Messpunkte (`{t, temp, soll}`).
 * @param {number} now        Aktuelle Zeit in ms seit Epoch.
 * @param {number} windowHours Anzeigedauer in Stunden.
 * @returns {Array} Neue, gefilterte Messpunktliste.
 */
export function pruneSamples(samples, now, windowHours) {
  if (!Array.isArray(samples)) return [];
  const hours = clampGraphHours(windowHours);
  const cutoff = now - hours * 3600 * 1000;
  return samples.filter(
    (s) => s && Number.isFinite(s.t) && s.t >= cutoff
  );
}

/**
 * Hängt einen Messpunkt an und beschneidet anschließend auf das Zeitfenster
 * (Req 13.1, 13.3). Liefert eine neue Liste (immutabel).
 *
 * @param {Array} samples     Bestehende Messpunkte.
 * @param {{t: number, temp: number, soll: (number|null)}} sample Neuer Punkt.
 * @param {number} windowHours Anzeigedauer in Stunden (für die Beschneidung).
 * @returns {Array} Neue Messpunktliste.
 */
export function appendSample(samples, sample, windowHours) {
  const base = Array.isArray(samples) ? samples : [];
  if (!sample || !Number.isFinite(sample.t) || !Number.isFinite(sample.temp)) {
    return base.slice();
  }
  const next = [...base, sample];
  return pruneSamples(next, sample.t, windowHours);
}

/**
 * Serialisiert die Messpunkte des Temperaturverlaufs zu einem JSON-String für
 * die benutzerbezogene Persistenz im HA-Benutzerspeicher (Req 13.12).
 *
 * @param {Array} samples Messpunkte (`{t, temp, soll}`).
 * @returns {string} JSON-String der Messpunktliste.
 */
export function serializeSamples(samples) {
  return JSON.stringify(Array.isArray(samples) ? samples : []);
}

/**
 * Parst persistierte Messpunkte robust aus dem HA-Benutzerspeicher (Req 13.12).
 *
 * Akzeptiert sowohl bereits geparste Arrays als auch JSON-Strings. Ungültiges
 * JSON, fehlender Wert oder Nicht-Array ergeben eine leere Liste `[]`. Einträge
 * ohne endliche `t`/`temp` werden verworfen; `soll` wird auf eine endliche Zahl
 * normalisiert oder `null`.
 *
 * @param {unknown} raw Rohwert (Array, JSON-String, null, …).
 * @returns {Array} Geparste, bereinigte Messpunktliste.
 */
export function parseSamples(raw) {
  let data = raw;
  if (typeof raw === 'string') {
    try {
      data = JSON.parse(raw);
    } catch {
      return [];
    }
  }
  if (!Array.isArray(data)) {
    return [];
  }
  return data
    .filter((s) => s && Number.isFinite(s.t) && Number.isFinite(s.temp))
    .map((s) => ({
      t: s.t,
      temp: s.temp,
      soll: Number.isFinite(s.soll) ? s.soll : null,
    }));
}

/**
 * Baut die Konfiguration für die eingebettete native `history-graph`-Karte
 * (Req 13). Die Card nutzt damit die echte Recorder-Historie von Home Assistant.
 * Die Ist-Temperatur (Sensor) und — sofern gesetzt — die Solltemperatur werden
 * als Linien angezeigt; die Solltemperatur wird in Rot gezeichnet
 * (`color: "red"`, unterstützt ab HA 2026.6).
 *
 * Nur Entitäten mit gültiger (nicht-leerer) ID werden aufgenommen. `hours`
 * steuert `hours_to_show` und darf **fraktional** sein (z. B. beim Begrenzen des
 * Fensters auf den Startzeitpunkt eines laufenden Brauvorgangs). Gültig ist jede
 * endliche Zahl > 0; sie wird auf maximal {@link MAX_GRAPH_HOURS} gedeckelt.
 * Ungültige/fehlende Werte fallen auf {@link DEFAULT_GRAPH_HOURS} zurück.
 *
 * @param {string} sensorEntity   Entity-ID des Temperatursensors (Ist).
 * @param {string} setpointEntity Entity-ID der Solltemperatur (z. B. input_number).
 * @param {number|string} hours   Anzeigedauer in Stunden (fraktional erlaubt, max. 4).
 * @param {{actual?: string, setpoint?: string}} [names] Anzeigenamen der Linien
 *   (sprachabhängig, Req 14). Fehlt ein Name, wird der englische Default genutzt.
 * @returns {Object} Gültige `history-graph`-Kartenkonfiguration.
 */
export function buildHistoryGraphConfig(sensorEntity, setpointEntity, hours, names = {}) {
  const actualName = names && names.actual ? names.actual : 'Actual temperature';
  const setpointName = names && names.setpoint ? names.setpoint : 'Target temperature';
  const entities = [];
  if (typeof sensorEntity === 'string' && sensorEntity.trim() !== '') {
    entities.push({ entity: sensorEntity, name: actualName });
  }
  if (typeof setpointEntity === 'string' && setpointEntity.trim() !== '') {
    entities.push({ entity: setpointEntity, name: setpointName, color: 'red' });
  }
  const num = typeof hours === 'string' ? Number(hours) : hours;
  const hoursToShow =
    typeof num === 'number' && Number.isFinite(num) && num > 0
      ? Math.min(num, MAX_GRAPH_HOURS)
      : DEFAULT_GRAPH_HOURS;
  return {
    type: 'history-graph',
    hours_to_show: hoursToShow,
    entities,
  };
}

/**
 * Berechnet aus den Messpunkten ein rein geometrisches Modell für das SVG-
 * Rendering des Temperaturverlaufs (Req 13.1, 13.2). Zwei Datenreihen werden
 * aufbereitet: die Ist-Temperatur (`tempPoints`) und — sofern vorhanden — die
 * Solltemperatur (`sollPoints`). Die y-Skala umfasst beide Reihen.
 *
 * Die Funktion ist rein: gleiche Eingaben ⇒ gleiche Ausgaben, keine
 * Seiteneffekte. x bildet die Zeit linear auf die Zeichenbreite ab, y die
 * Temperatur (invertiert, da SVG-y nach unten wächst). Bei leerer Liste wird
 * `{ empty: true, ... }` geliefert; bei nur einem Punkt oder konstantem Wert
 * werden Division-durch-Null-Fälle sicher behandelt.
 *
 * @param {Array} samples Messpunkte (`{t, temp, soll}`).
 * @param {Object} [options] Zeichenmaße und Achsen-Parameter.
 * @param {number} [options.width=400]
 * @param {number} [options.height=160]
 * @param {number} [options.padLeft=40]
 * @param {number} [options.padRight=10]
 * @param {number} [options.padTop=8]
 * @param {number} [options.padBottom=28]
 * @param {number} [options.windowHours] Anzeigedauer in Stunden; ist sie gesetzt,
 *   spannt die Zeitachse das feste Fenster `[now - windowHours, now]` auf
 *   (stabiler „Maßstab"). Ohne Angabe wird der Datenbereich verwendet.
 * @param {number} [options.now] Bezugszeitpunkt (ms) für das Zeitfenster
 *   (Default: Zeit des jüngsten Messpunkts).
 * @returns {Object} Geometriemodell mit `tempPoints`/`sollPoints` ({x,y}),
 *   Wertebereich (`minVal`/`maxVal`), Zeitbereich (`tMin`/`tMax`) sowie
 *   beschrifteten Achsenmarken `xTicks` ({x, label "hh:mm"}) und `yTicks`
 *   ({y, value, label}).
 */
export function buildGraphModel(samples, options = {}) {
  const {
    width = 400,
    height = 160,
    padLeft = 40,
    padRight = 10,
    padTop = 8,
    padBottom = 28,
    windowHours = null,
    now = null,
  } = options;

  const meta = { width, height, padLeft, padRight, padTop, padBottom };

  const list = (Array.isArray(samples) ? samples : []).filter(
    (s) => s && Number.isFinite(s.t) && Number.isFinite(s.temp)
  );

  const x0 = padLeft;
  const x1 = width - padRight;
  const y0 = padTop;
  const y1 = height - padBottom;

  // --- Zeitachse (x): festes Fenster bei gesetztem windowHours, sonst Datenbereich ---
  let tMin;
  let tMax;
  if (Number.isFinite(windowHours) || typeof windowHours === 'string') {
    const hours = clampGraphHours(windowHours);
    const ref = Number.isFinite(now)
      ? now
      : list.length > 0
        ? list[list.length - 1].t
        : Date.now();
    tMax = ref;
    tMin = ref - hours * 3600 * 1000;
  } else if (list.length > 0) {
    tMin = list[0].t;
    tMax = list[list.length - 1].t;
  } else {
    tMin = null;
    tMax = null;
  }
  const tSpan = tMin !== null ? tMax - tMin : 0;
  const xOf = (t) => (tSpan === 0 ? x0 : x0 + ((t - tMin) / tSpan) * (x1 - x0));

  // --- Zeitachsen-Marken: absolute Uhrzeit hh:mm im dauerabhängigen Raster (Req 13.9) ---
  let xTicks = [];
  if (tMin !== null && tSpan > 0) {
    const stepMin = graphTimeStepMinutes(
      Number.isFinite(windowHours) || typeof windowHours === 'string'
        ? windowHours
        : Math.max(1, Math.round(tSpan / 3600000))
    );
    const stepMs = stepMin * 60 * 1000;
    // Erste Marke auf das nächste „runde" Vielfache von stepMin nach tMin legen.
    const firstTick = Math.ceil(tMin / stepMs) * stepMs;
    for (let t = firstTick; t <= tMax + 1; t += stepMs) {
      xTicks.push({ x: xOf(t), t, label: formatClock(t) });
    }
  }

  if (list.length === 0) {
    return {
      empty: true,
      tempPoints: [],
      sollPoints: [],
      minVal: null,
      maxVal: null,
      tMin,
      tMax,
      xTicks,
      yTicks: [],
      plot: { x0, x1, y0, y1 },
      ...meta,
    };
  }

  let minVal = Infinity;
  let maxVal = -Infinity;
  for (const s of list) {
    if (s.temp < minVal) minVal = s.temp;
    if (s.temp > maxVal) maxVal = s.temp;
    if (Number.isFinite(s.soll)) {
      if (s.soll < minVal) minVal = s.soll;
      if (s.soll > maxVal) maxVal = s.soll;
    }
  }
  if (minVal === maxVal) {
    minVal -= 1;
    maxVal += 1;
  }

  // --- Temperaturachse (y): runde Marken, die den Wertebereich umschließen (Req 13.10) ---
  const ticksVals = niceTicks(minVal, maxVal, 5);
  // Skala an die runden Marken anpassen, damit Beschriftung und Kurve passen.
  const axisMin = ticksVals.length > 0 ? Math.min(ticksVals[0], minVal) : minVal;
  const axisMax =
    ticksVals.length > 0 ? Math.max(ticksVals[ticksVals.length - 1], maxVal) : maxVal;
  const vSpan = axisMax - axisMin || 1;
  const yOf = (v) => y1 - ((v - axisMin) / vSpan) * (y1 - y0);

  const yTicks = ticksVals.map((v) => ({ y: yOf(v), value: v, label: `${v}°` }));

  const tempPoints = list.map((s) => ({ x: xOf(s.t), y: yOf(s.temp) }));
  const sollPoints = list
    .filter((s) => Number.isFinite(s.soll))
    .map((s) => ({ x: xOf(s.t), y: yOf(s.soll) }));

  return {
    empty: false,
    tempPoints,
    sollPoints,
    minVal: axisMin,
    maxVal: axisMax,
    tMin,
    tMax,
    xTicks,
    yTicks,
    plot: { x0, x1, y0, y1 },
    ...meta,
  };
}

// ---------------------------------------------------------------------------
// Internationalisierung (i18n) — EN/DE (Req 14)
// ---------------------------------------------------------------------------
//
// Reine, HA-/DOM-freie Übersetzungshilfen. Die Karte liest die Sprache aus dem
// Snapshot der Integration (`settings.language`, Single Source of Truth), sodass
// auch die Benachrichtigungen der Integration dieselbe Sprache verwenden.

/** Standardsprache (Req 14: Default Englisch). @type {string} */
export const DEFAULT_LANGUAGE = 'en';

/** Unterstützte Sprachen. @type {string[]} */
export const SUPPORTED_LANGUAGES = ['en', 'de'];

/**
 * Normalisiert einen (möglicherweise ungültigen) Sprachwert auf eine
 * unterstützte Sprache; fällt auf {@link DEFAULT_LANGUAGE} zurück.
 * @param {string|null|undefined} raw Rohwert (z. B. Helferzustand).
 * @returns {string} 'en' oder 'de'.
 */
export function resolveLanguage(raw) {
  return SUPPORTED_LANGUAGES.includes(raw) ? raw : DEFAULT_LANGUAGE;
}

/** Unterstützte Temperatureinheiten (nur Anzeige, keine Umrechnung) — Req 15. @type {string[]} */
export const SUPPORTED_UNITS = ['°C', '°F'];

/** Standard-Temperatureinheit. @type {string} */
export const DEFAULT_UNIT = '°C';

/**
 * Normalisiert einen (möglicherweise ungültigen) Einheitenwert auf `°C`/`°F`;
 * fällt auf {@link DEFAULT_UNIT} zurück (Req 15). Es findet KEINE Umrechnung
 * statt — die Einheit ist reine Beschriftung.
 * @param {string|null|undefined} raw Rohwert (z. B. Helferzustand).
 * @returns {string} '°C' oder '°F'.
 */
export function resolveUnit(raw) {
  return SUPPORTED_UNITS.includes(raw) ? raw : DEFAULT_UNIT;
}

/**
 * Übersetzungstabelle. Jeder Schlüssel existiert in beiden Sprachen.
 * Platzhalter im Format `{name}` werden über `vars` ersetzt.
 * @type {{en: Object<string,string>, de: Object<string,string>}}
 */
export const TRANSLATIONS = Object.freeze({
  en: {
    app_title: '🍺 Brew Control',
    manage_recipes_tt: 'Manage recipes',
    settings_tt: 'Settings',
    language_tt: 'Language',
    // Status
    no_sensor: '⚠️ No sensor set',
    invalid_sensor: '⚠️ No valid sensor value',
    safety_shutoff: '🛡 Safety shutoff at {v} {unit}',
    safety_shutoff_delta: '🛡 Safety shutoff at 𝚫t {v} {unit}',
    safety_shutoff_none: '🛡 Safety shutoff at —',
    hysteresis_status: '🌡 Hysteresis: {v} {unit}',
    status_idle: 'idle',
    status_running: 'running',
    status_paused: 'paused',
    status_done: 'done',
    // Heater
    heater_turn_off_tt: 'Turn heater off',
    heater_turn_on_tt: 'Turn heater on',
    heater_on: 'ON',
    heater_off: 'OFF',
    // Countdown
    remaining_hold: '⏱ Remaining hold time: {v}',
    // Graph
    graph_title: '📈 Temperature history',
    graph_actual: 'Actual temperature',
    graph_setpoint: 'Target temperature',
    duration: 'Duration:',
    select_sensor_first: 'Please select a temperature sensor via ⚙️ first.',
    loading_graph: 'Loading temperature history…',
    // Controls
    next_rest: '⏭ Next rest',
    stop: '⏹ Stop',
    start: '▶ Start',
    start_disabled_tt: 'Start requires at least one rest and a valid sensor value.',
    start_tt: 'Start brewing process',
    // Recipe
    no_rests: 'No rests yet. Add a rest below.',
    clear_all: '🗑 Clear all',
    move_up_tt: 'Move up',
    move_down_tt: 'Move down',
    edit_tt: 'Edit',
    delete_tt: 'Delete',
    name_optional: 'Name (optional)',
    save: '✓ Save',
    cancel: '✕ Cancel',
    add_rest: '＋ Rest',
    close: '✕ Close',
    step_detail: '{temp} {unit} · {dur} min',
    // Library
    manage_recipes_title: '📋 Manage recipes',
    no_saved_recipes: 'No saved recipes yet.',
    rests_count: '{n} rests',
    load_recipe_tt: 'Load as active recipe',
    load: '📥 Load',
    delete_from_library_tt: 'Delete from library',
    save_current_as: 'Save current recipe as…',
    recipe_name_placeholder: 'e.g. Pale Ale',
    save_as: '💾 Save',
    overwrite_confirm: 'Recipe "{name}" already exists. Overwrite?',
    // Active recipe name (Req 16)
    new_recipe: 'New recipe',
    modified_suffix: 'New',
    active_recipe_tt: 'Active recipe',
    // Settings
    sensor_label: 'Temperature sensor',
    heater_label: 'Heater switch',
    hysteresis_label: 'Hysteresis ({unit}, > 0 to 5)',
    unit_label: 'Temperature unit',
    // Errors
    err_lib_load: 'Recipe library could not be loaded.',
    err_lib_save: 'Recipe library could not be saved.',
    err_enter_name: 'Please enter a name for the recipe.',
    err_recipe_not_found: 'Recipe not found.',
    err_invalid_step_add: 'Invalid input: target temperature must be 0–100 °C, hold time a whole number > 0 minutes.',
    err_invalid_step_edit: 'Invalid input: change discarded. Target temperature 0–100 °C, hold time a whole number > 0.',
    err_no_heater: 'No heater switch set.',
    err_heater_toggle: 'Switching the heater failed.',
    err_invalid_hysteresis: 'Invalid hysteresis: please enter a value greater than 0 °C up to 5 °C.',
    err_invalid_entity: 'Invalid entity ID: the sensor must be a sensor.… entity and the heater a switch.… entity (lowercase letters, digits, _).',
    err_not_loaded: 'HA-Beer-Brew-Control integration is not loaded. Please check Settings → Devices & services.',
    err_admin_required: 'This setting can only be changed by an administrator.',
    err_recipe_locked: 'The recipe cannot be changed while brewing is running.',
    err_settings_locked: 'Sensor and heater cannot be changed while brewing is running.',
    err_too_many_rests: 'Too many rests (max. 30).',
    err_library_full: 'Recipe library is full. Please delete a recipe first.',
    err_invalid_name: 'Invalid recipe name (1–64 characters, no control characters).',
    err_name_exists: 'A recipe with this name already exists.',
    err_already_running: 'Brewing is already running.',
    err_not_running: 'Brewing is not running.',
    err_recipe_empty: 'The recipe has no rests.',
    err_not_configured: 'Sensor and heater are not configured.',
    err_heater_unavailable: 'The heater switch is unavailable.',
    err_sensor_invalid: 'The temperature sensor has no valid value.',
    err_legacy_automations_active: 'Old Brew Control automations are still active. Please disable or remove them first.',
    err_storage_error: 'Saving failed. Please try again.',
    err_internal_error: 'Internal error. The heater was switched off for safety.',
    err_generic: 'Action failed ({code}).',
    err_version_mismatch: 'Card version {card} does not match integration version {server}. Please reload the page.',
    import_skipped: '{n} recipes from the old library could not be imported.',
    // Editor
    editor_note: 'Configuration via Settings → Devices & services → HA-Beer-Brew-Control or ⚙️ in the card.',
  },
  de: {
    app_title: '🍺 Brausteuerung',
    manage_recipes_tt: 'Rezepte verwalten',
    settings_tt: 'Einstellungen',
    language_tt: 'Sprache',
    no_sensor: '⚠️ Kein Sensor gesetzt',
    invalid_sensor: '⚠️ Kein gültiger Sensorwert',
    safety_shutoff: '🛡 Sicherheitsabschaltung bei {v} {unit}',
    safety_shutoff_delta: '🛡 Sicherheitsabschaltung bei 𝚫t {v} {unit}',
    safety_shutoff_none: '🛡 Sicherheitsabschaltung bei —',
    hysteresis_status: '🌡 Hysterese: {v} {unit}',
    status_idle: 'bereit',
    status_running: 'läuft',
    status_paused: 'pausiert',
    status_done: 'fertig',
    heater_turn_off_tt: 'Heizung ausschalten',
    heater_turn_on_tt: 'Heizung einschalten',
    heater_on: 'AN',
    heater_off: 'AUS',
    remaining_hold: '⏱ Verbleibende Haltezeit: {v}',
    graph_title: '📈 Temperaturverlauf',
    graph_actual: 'Ist-Temperatur',
    graph_setpoint: 'Solltemperatur',
    duration: 'Dauer:',
    select_sensor_first: 'Bitte zuerst über ⚙️ einen Temperatursensor auswählen.',
    loading_graph: 'Lade Temperaturverlauf…',
    next_rest: '⏭ Nächste Rast',
    stop: '⏹ Stopp',
    start: '▶ Start',
    start_disabled_tt: 'Start benötigt mindestens eine Rast und einen gültigen Sensorwert.',
    start_tt: 'Brauprozess starten',
    no_rests: 'Noch keine Raststufen. Unten eine Rast hinzufügen.',
    clear_all: '🗑 Alle löschen',
    move_up_tt: 'Nach oben',
    move_down_tt: 'Nach unten',
    edit_tt: 'Bearbeiten',
    delete_tt: 'Löschen',
    name_optional: 'Name (optional)',
    save: '✓ Speichern',
    cancel: '✕ Abbrechen',
    add_rest: '＋ Rast',
    close: '✕ Schließen',
    step_detail: '{temp} {unit} · {dur} min',
    manage_recipes_title: '📋 Rezepte verwalten',
    no_saved_recipes: 'Noch keine gespeicherten Rezepte.',
    rests_count: '{n} Rasten',
    load_recipe_tt: 'Als aktives Rezept laden',
    load: '📥 Laden',
    delete_from_library_tt: 'Aus Bibliothek löschen',
    save_current_as: 'Aktuelles Rezept speichern unter…',
    recipe_name_placeholder: 'z. B. Helles',
    save_as: '💾 Speichern',
    overwrite_confirm: 'Rezept "{name}" existiert bereits. Überschreiben?',
    new_recipe: 'Neues Rezept',
    modified_suffix: 'Neu',
    active_recipe_tt: 'Aktives Rezept',
    sensor_label: 'Temperatursensor',
    heater_label: 'Heizungs-Aktor',
    hysteresis_label: 'Hysterese ({unit}, > 0 bis 5)',
    unit_label: 'Temperatureinheit',
    err_lib_load: 'Rezept-Bibliothek konnte nicht geladen werden.',
    err_lib_save: 'Rezept-Bibliothek konnte nicht gespeichert werden.',
    err_enter_name: 'Bitte einen Namen für das Rezept eingeben.',
    err_recipe_not_found: 'Rezept nicht gefunden.',
    err_invalid_step_add: 'Ungültige Eingabe: Solltemperatur muss 0–100 °C sein, Haltezeit eine ganze Zahl > 0 Minuten.',
    err_invalid_step_edit: 'Ungültige Eingabe: Änderung verworfen. Solltemperatur 0–100 °C, Haltezeit ganze Zahl > 0.',
    err_no_heater: 'Kein Heizungs-Aktor gesetzt.',
    err_heater_toggle: 'Schalten des Heizungs-Aktors fehlgeschlagen.',
    err_invalid_hysteresis: 'Ungültige Hysterese: bitte einen Wert größer als 0 °C bis maximal 5 °C eingeben.',
    err_invalid_entity: 'Ungültige Entity-ID: der Sensor muss eine sensor.…-Entität sein, die Heizung eine switch.…-Entität (Kleinbuchstaben, Ziffern, _).',
    err_not_loaded: 'Die Integration HA-Beer-Brew-Control ist nicht geladen. Bitte unter Einstellungen → Geräte & Dienste prüfen.',
    err_admin_required: 'Diese Einstellung kann nur ein Administrator ändern.',
    err_recipe_locked: 'Das Rezept kann während eines laufenden Brauvorgangs nicht geändert werden.',
    err_settings_locked: 'Sensor und Heizung können während eines laufenden Brauvorgangs nicht geändert werden.',
    err_too_many_rests: 'Zu viele Rasten (max. 30).',
    err_library_full: 'Die Rezept-Bibliothek ist voll. Bitte zuerst ein Rezept löschen.',
    err_invalid_name: 'Ungültiger Rezeptname (1–64 Zeichen, keine Steuerzeichen).',
    err_name_exists: 'Ein Rezept mit diesem Namen existiert bereits.',
    err_already_running: 'Der Brauvorgang läuft bereits.',
    err_not_running: 'Es läuft kein Brauvorgang.',
    err_recipe_empty: 'Das Rezept enthält keine Rasten.',
    err_not_configured: 'Sensor und Heizung sind nicht konfiguriert.',
    err_heater_unavailable: 'Der Heizungs-Aktor ist nicht verfügbar.',
    err_sensor_invalid: 'Der Temperatursensor liefert keinen gültigen Wert.',
    err_legacy_automations_active: 'Alte Brausteuerung-Automationen sind noch aktiv. Bitte zuerst deaktivieren oder entfernen.',
    err_storage_error: 'Speichern fehlgeschlagen. Bitte erneut versuchen.',
    err_internal_error: 'Interner Fehler. Die Heizung wurde sicherheitshalber ausgeschaltet.',
    err_generic: 'Aktion fehlgeschlagen ({code}).',
    err_version_mismatch: 'Kartenversion {card} passt nicht zur Integrationsversion {server}. Bitte die Seite neu laden.',
    import_skipped: '{n} Rezepte aus der alten Bibliothek konnten nicht übernommen werden.',
    editor_note: 'Konfiguration über Einstellungen → Geräte & Dienste → HA-Beer-Brew-Control bzw. ⚙️ in der Karte.',
  },
});

/**
 * Übersetzt einen Schlüssel in die gewählte Sprache (Req 14). Unbekannte
 * Sprachen fallen auf {@link DEFAULT_LANGUAGE}, unbekannte Schlüssel auf die
 * englische Fassung bzw. den Schlüssel selbst zurück. Platzhalter `{name}`
 * werden aus `vars` ersetzt.
 *
 * @param {string} lang Sprachcode ('en'/'de').
 * @param {string} key  Übersetzungsschlüssel.
 * @param {Object<string, (string|number)>} [vars] Platzhalterwerte.
 * @returns {string} Übersetzter Text.
 */
export function translate(lang, key, vars) {
  const l = resolveLanguage(lang);
  const table = TRANSLATIONS[l] || TRANSLATIONS[DEFAULT_LANGUAGE];
  let str =
    table[key] !== undefined
      ? table[key]
      : TRANSLATIONS[DEFAULT_LANGUAGE][key] !== undefined
        ? TRANSLATIONS[DEFAULT_LANGUAGE][key]
        : key;
  if (vars) {
    str = str.replace(/\{(\w+)\}/g, (m, k) => (vars[k] !== undefined ? String(vars[k]) : m));
  }
  return str;
}
