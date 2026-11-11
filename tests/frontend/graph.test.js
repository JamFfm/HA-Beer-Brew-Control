import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import {
  clampGraphHours,
  shouldRecordSample,
  pruneSamples,
  appendSample,
  buildGraphModel,
  buildHistoryGraphConfig,
  MIN_GRAPH_HOURS,
  MAX_GRAPH_HOURS,
  DEFAULT_GRAPH_HOURS,
  Status,
} from '../../custom_components/brausteuerung/frontend/brausteuerung-logic.js';

// Feature: brausteuerung, Temperaturverlauf-Graph (Req 13)

describe('clampGraphHours — Anzeigedauer 1..4 h, Default 2 h (Req 13.3)', () => {
  it('gültige Ganzzahlen 1..4 werden unverändert übernommen', () => {
    for (const h of [1, 2, 3, 4]) {
      expect(clampGraphHours(h)).toBe(h);
      expect(clampGraphHours(String(h))).toBe(h);
    }
  });

  it('Werte außerhalb 1..4 werden auf die Grenzen geklemmt', () => {
    expect(clampGraphHours(0)).toBe(MIN_GRAPH_HOURS);
    expect(clampGraphHours(-5)).toBe(MIN_GRAPH_HOURS);
    expect(clampGraphHours(5)).toBe(MAX_GRAPH_HOURS);
    expect(clampGraphHours(100)).toBe(MAX_GRAPH_HOURS);
  });

  it('fehlende/ungültige Werte ⇒ Default 2 h', () => {
    for (const v of [undefined, null, NaN, Infinity, 'abc', '']) {
      expect(clampGraphHours(v)).toBe(DEFAULT_GRAPH_HOURS);
    }
  });

  it('Property: Ergebnis ist stets eine Ganzzahl in [1,4]', () => {
    fc.assert(
      fc.property(fc.double({ min: -1000, max: 1000, noNaN: true }), (v) => {
        const r = clampGraphHours(v);
        expect(Number.isInteger(r)).toBe(true);
        expect(r).toBeGreaterThanOrEqual(MIN_GRAPH_HOURS);
        expect(r).toBeLessThanOrEqual(MAX_GRAPH_HOURS);
      }),
      { numRuns: 200 }
    );
  });
});

describe('shouldRecordSample — Aufzeichnung nur bei running oder Daueranzeige (Req 13.4, 13.6)', () => {
  it('running ⇒ immer aufzeichnen', () => {
    expect(shouldRecordSample(Status.RUNNING, false)).toBe(true);
    expect(shouldRecordSample(Status.RUNNING, true)).toBe(true);
  });

  it('nicht running ⇒ nur bei aktivierter Daueranzeige', () => {
    expect(shouldRecordSample(Status.IDLE, false)).toBe(false);
    expect(shouldRecordSample(Status.IDLE, true)).toBe(true);
    expect(shouldRecordSample(Status.DONE, false)).toBe(false);
    expect(shouldRecordSample(Status.PAUSED, true)).toBe(true);
  });
});

describe('pruneSamples — entfernt Punkte außerhalb des Zeitfensters (Req 13.3)', () => {
  it('behält nur Punkte innerhalb von windowHours vor now', () => {
    const now = 10_000_000;
    const hour = 3600 * 1000;
    const samples = [
      { t: now - 5 * hour, temp: 20, soll: null }, // zu alt (Fenster 2h)
      { t: now - 1.5 * hour, temp: 40, soll: 67 },
      { t: now - 0.5 * hour, temp: 60, soll: 67 },
    ];
    const pruned = pruneSamples(samples, now, 2);
    expect(pruned).toHaveLength(2);
    expect(pruned[0].temp).toBe(40);
    expect(pruned[1].temp).toBe(60);
  });

  it('ist immutabel und robust gegen Nicht-Arrays', () => {
    const samples = [{ t: 1, temp: 1, soll: null }];
    const copy = [...samples];
    pruneSamples(samples, 1, 2);
    expect(samples).toEqual(copy);
    expect(pruneSamples(null, 1, 2)).toEqual([]);
    expect(pruneSamples(undefined, 1, 2)).toEqual([]);
  });
});

describe('appendSample — hängt an und beschneidet (Req 13.1, 13.3)', () => {
  it('fügt einen gültigen Punkt hinzu', () => {
    const out = appendSample([], { t: 1000, temp: 50, soll: 67 }, 2);
    expect(out).toHaveLength(1);
    expect(out[0]).toEqual({ t: 1000, temp: 50, soll: 67 });
  });

  it('ignoriert ungültige Punkte (NaN-Zeit/Temp)', () => {
    expect(appendSample([], { t: NaN, temp: 50, soll: null }, 2)).toEqual([]);
    expect(appendSample([], { t: 1, temp: NaN, soll: null }, 2)).toEqual([]);
    expect(appendSample([], null, 2)).toEqual([]);
  });

  it('beschneidet alte Punkte beim Anhängen', () => {
    const hour = 3600 * 1000;
    const t0 = 0;
    const old = { t: t0, temp: 20, soll: null };
    const recent = { t: t0 + 5 * hour, temp: 70, soll: 67 };
    // Fenster 2h, neuer Punkt 5h nach dem alten ⇒ alter fällt raus.
    const out = appendSample([old], recent, 2);
    expect(out).toHaveLength(1);
    expect(out[0].temp).toBe(70);
  });

  it('mutiert die Eingabeliste nicht', () => {
    const samples = [{ t: 1, temp: 1, soll: null }];
    const copy = [...samples];
    appendSample(samples, { t: 2, temp: 2, soll: null }, 2);
    expect(samples).toEqual(copy);
  });
});

describe('buildGraphModel — reines Geometriemodell (Req 13.1, 13.2)', () => {
  it('leere Liste ⇒ empty: true, keine Punkte', () => {
    const m = buildGraphModel([]);
    expect(m.empty).toBe(true);
    expect(m.tempPoints).toEqual([]);
    expect(m.sollPoints).toEqual([]);
  });

  it('zeichnet Ist- und Soll-Reihe; Soll nur für Punkte mit soll-Wert', () => {
    const samples = [
      { t: 0, temp: 20, soll: 67 },
      { t: 1000, temp: 40, soll: 67 },
      { t: 2000, temp: 67, soll: null },
    ];
    const m = buildGraphModel(samples, { width: 400, height: 160 });
    expect(m.empty).toBe(false);
    expect(m.tempPoints).toHaveLength(3);
    expect(m.sollPoints).toHaveLength(2); // nur die mit soll != null
    // y-Achse rastet auf runde Marken; der Wertebereich (20..67) wird umschlossen.
    expect(m.minVal).toBeLessThanOrEqual(20);
    expect(m.maxVal).toBeGreaterThanOrEqual(67);
  });

  it('alle Punkte liegen innerhalb der Zeichenfläche', () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            t: fc.integer({ min: 0, max: 1_000_000 }),
            temp: fc.double({ min: 0, max: 100, noNaN: true }),
            soll: fc.option(fc.double({ min: 0, max: 100, noNaN: true }), { nil: null }),
          }),
          { minLength: 1, maxLength: 50 }
        ),
        (rawSamples) => {
          // nach Zeit sortieren (so wie die Card sie aufzeichnet)
          const samples = [...rawSamples].sort((a, b) => a.t - b.t);
          const width = 400;
          const height = 160;
          const m = buildGraphModel(samples, { width, height });
          for (const p of [...m.tempPoints, ...m.sollPoints]) {
            expect(p.x).toBeGreaterThanOrEqual(0);
            expect(p.x).toBeLessThanOrEqual(width);
            expect(p.y).toBeGreaterThanOrEqual(0);
            expect(p.y).toBeLessThanOrEqual(height);
          }
        }
      ),
      { numRuns: 200 }
    );
  });

  it('konstanter Wert erzeugt keine Division durch Null', () => {
    const samples = [
      { t: 0, temp: 50, soll: null },
      { t: 1000, temp: 50, soll: null },
    ];
    const m = buildGraphModel(samples);
    for (const p of m.tempPoints) {
      expect(Number.isFinite(p.x)).toBe(true);
      expect(Number.isFinite(p.y)).toBe(true);
    }
  });
});

describe('buildHistoryGraphConfig — native history-graph-Konfiguration (Req 13)', () => {
  it('baut Ist- und Solltemperatur als Entitäten, Soll in Rot (englische Default-Namen)', () => {
    const cfg = buildHistoryGraphConfig('sensor.brau_temp', 'input_number.brau_solltemperatur', 2);
    expect(cfg.type).toBe('history-graph');
    expect(cfg.hours_to_show).toBe(2);
    expect(cfg.entities).toEqual([
      { entity: 'sensor.brau_temp', name: 'Actual temperature' },
      { entity: 'input_number.brau_solltemperatur', name: 'Target temperature', color: 'red' },
    ]);
  });

  it('übernimmt sprachabhängige Linien-Namen aus dem names-Parameter', () => {
    const cfg = buildHistoryGraphConfig('sensor.x', 'input_number.y', 2, {
      actual: 'Ist-Temperatur',
      setpoint: 'Solltemperatur',
    });
    expect(cfg.entities).toEqual([
      { entity: 'sensor.x', name: 'Ist-Temperatur' },
      { entity: 'input_number.y', name: 'Solltemperatur', color: 'red' },
    ]);
  });

  it('erlaubt fraktionale Stunden und deckelt auf 4 h; ungültige ⇒ Default', () => {
    expect(buildHistoryGraphConfig('sensor.x', 'input_number.y', 0.25).hours_to_show).toBeCloseTo(0.25);
    expect(buildHistoryGraphConfig('sensor.x', 'input_number.y', 99).hours_to_show).toBe(MAX_GRAPH_HOURS);
    expect(buildHistoryGraphConfig('sensor.x', 'input_number.y', 0).hours_to_show).toBe(DEFAULT_GRAPH_HOURS);
    expect(buildHistoryGraphConfig('sensor.x', 'input_number.y', -1).hours_to_show).toBe(DEFAULT_GRAPH_HOURS);
    expect(buildHistoryGraphConfig('sensor.x', 'input_number.y', 'abc').hours_to_show).toBe(DEFAULT_GRAPH_HOURS);
  });

  it('nimmt nur Entitäten mit gültiger, nicht-leerer ID auf', () => {
    expect(buildHistoryGraphConfig('', '', 2).entities).toEqual([]);
    expect(buildHistoryGraphConfig('sensor.x', '', 2).entities).toEqual([
      { entity: 'sensor.x', name: 'Actual temperature' },
    ]);
    expect(buildHistoryGraphConfig('   ', 'input_number.y', 2).entities).toEqual([
      { entity: 'input_number.y', name: 'Target temperature', color: 'red' },
    ]);
  });
});

import {
  translate,
  resolveLanguage,
  resolveUnit,
  DEFAULT_LANGUAGE,
  SUPPORTED_LANGUAGES,
  DEFAULT_UNIT,
  SUPPORTED_UNITS,
} from '../../custom_components/brausteuerung/frontend/brausteuerung-logic.js';

describe('i18n — resolveLanguage / translate (Req 14)', () => {
  it('resolveLanguage akzeptiert en/de, sonst Default en', () => {
    expect(resolveLanguage('en')).toBe('en');
    expect(resolveLanguage('de')).toBe('de');
    expect(resolveLanguage('fr')).toBe(DEFAULT_LANGUAGE);
    expect(resolveLanguage(undefined)).toBe(DEFAULT_LANGUAGE);
    expect(DEFAULT_LANGUAGE).toBe('en');
    expect(SUPPORTED_LANGUAGES).toEqual(['en', 'de']);
  });

  it('übersetzt Schlüssel in EN und DE', () => {
    expect(translate('en', 'start')).toBe('▶ Start');
    expect(translate('en', 'next_rest')).toBe('⏭ Next rest');
    expect(translate('de', 'next_rest')).toBe('⏭ Nächste Rast');
    expect(translate('en', 'settings_tt')).toBe('Settings');
    expect(translate('de', 'settings_tt')).toBe('Einstellungen');
  });

  it('ersetzt Platzhalter über vars', () => {
    expect(translate('en', 'safety_shutoff', { v: 77, unit: '°C' })).toBe('🛡 Safety shutoff at 77 °C');
    expect(translate('en', 'safety_shutoff', { v: 170, unit: '°F' })).toBe('🛡 Safety shutoff at 170 °F');
    expect(translate('de', 'safety_shutoff', { v: 77, unit: '°C' })).toBe('🛡 Sicherheitsabschaltung bei 77 °C');
    expect(translate('en', 'safety_shutoff_delta', { v: 10, unit: '°C' })).toBe('🛡 Safety shutoff at 𝚫t 10 °C');
    expect(translate('de', 'safety_shutoff_delta', { v: 18, unit: '°F' })).toBe('🛡 Sicherheitsabschaltung bei 𝚫t 18 °F');
    expect(translate('de', 'rests_count', { n: 3 })).toBe('3 Rasten');
    expect(translate('en', 'overwrite_confirm', { name: 'IPA' })).toBe(
      'Recipe "IPA" already exists. Overwrite?'
    );
  });

  it('ungültige Sprache fällt auf EN zurück; unbekannter Schlüssel bleibt der Schlüssel', () => {
    expect(translate('fr', 'start')).toBe('▶ Start');
    expect(translate('en', 'nicht_vorhanden')).toBe('nicht_vorhanden');
  });
});

describe('i18n — resolveUnit (Req 15)', () => {
  it('akzeptiert °C/°F, sonst Default °C', () => {
    expect(resolveUnit('°C')).toBe('°C');
    expect(resolveUnit('°F')).toBe('°F');
    expect(resolveUnit('K')).toBe(DEFAULT_UNIT);
    expect(resolveUnit(undefined)).toBe(DEFAULT_UNIT);
    expect(DEFAULT_UNIT).toBe('°C');
    expect(SUPPORTED_UNITS).toEqual(['°C', '°F']);
  });

  it('Einheit wird in Labels über {unit} eingesetzt', () => {
    expect(translate('de', 'hysteresis_label', { unit: '°F' })).toBe('Hysterese (°F, > 0 bis 5)');
    expect(translate('en', 'step_detail', { temp: 67, dur: 60, unit: '°F' })).toBe('67 °F · 60 min');
  });
});
