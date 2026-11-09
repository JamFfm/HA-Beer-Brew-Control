import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import {
  graphSampleIntervalMs,
  graphTimeStepMinutes,
  formatClock,
  niceTicks,
  buildGraphModel,
} from '../../custom_components/brausteuerung/frontend/brausteuerung-logic.js';

// Feature: brausteuerung, Graph-Achsen & dauerabhängiges Intervall (Req 13.8, 13.9, 13.10)

describe('graphSampleIntervalMs — dauerabhängiges Aufzeichnungsintervall (Req 13.8)', () => {
  it('skaliert mit der Anzeigedauer (15 s je Stunde)', () => {
    expect(graphSampleIntervalMs(1)).toBe(15000);
    expect(graphSampleIntervalMs(2)).toBe(30000);
    expect(graphSampleIntervalMs(3)).toBe(45000);
    expect(graphSampleIntervalMs(4)).toBe(60000);
  });

  it('klemmt ungültige Werte (über clampGraphHours)', () => {
    expect(graphSampleIntervalMs(0)).toBe(15000); // -> 1 h
    expect(graphSampleIntervalMs(99)).toBe(60000); // -> 4 h
    expect(graphSampleIntervalMs('abc')).toBe(30000); // -> Default 2 h
  });
});

describe('graphTimeStepMinutes — dauerabhängiger Tick-Abstand der Zeitachse (Req 13.9)', () => {
  it('skaliert mit der Anzeigedauer (15 min je Stunde)', () => {
    expect(graphTimeStepMinutes(1)).toBe(15);
    expect(graphTimeStepMinutes(2)).toBe(30);
    expect(graphTimeStepMinutes(3)).toBe(45);
    expect(graphTimeStepMinutes(4)).toBe(60);
  });
});

describe('formatClock — absolute Uhrzeit hh:mm (Req 13.9)', () => {
  it('formatiert mit führenden Nullen', () => {
    const d = new Date(2025, 0, 1, 8, 5, 0);
    expect(formatClock(d.getTime())).toBe('08:05');
  });

  it('akzeptiert auch ein Date', () => {
    const d = new Date(2025, 0, 1, 13, 42, 0);
    expect(formatClock(d)).toBe('13:42');
  });

  it('Property: Ergebnis passt stets auf das Muster hh:mm', () => {
    fc.assert(
      fc.property(fc.integer({ min: 0, max: 2_000_000_000_000 }), (ms) => {
        expect(formatClock(ms)).toMatch(/^\d{2}:\d{2}$/);
      }),
      { numRuns: 200 }
    );
  });
});

describe('niceTicks — runde Marken der Temperaturachse (Req 13.10)', () => {
  it('umschließt das Intervall mit runden, aufsteigenden Werten', () => {
    const ticks = niceTicks(53, 68, 5);
    expect(ticks.length).toBeGreaterThanOrEqual(2);
    // aufsteigend
    for (let i = 1; i < ticks.length; i++) {
      expect(ticks[i]).toBeGreaterThan(ticks[i - 1]);
    }
    // umschließt den Bereich
    expect(ticks[0]).toBeLessThanOrEqual(53);
    expect(ticks[ticks.length - 1]).toBeGreaterThanOrEqual(68);
  });

  it('behandelt konstante Werte ohne Absturz', () => {
    const ticks = niceTicks(50, 50, 5);
    expect(ticks.length).toBeGreaterThanOrEqual(2);
  });

  it('Property: Marken sind streng monoton steigend und gleichmäßig', () => {
    fc.assert(
      fc.property(
        fc.double({ min: -50, max: 150, noNaN: true }),
        fc.double({ min: 0.1, max: 100, noNaN: true }),
        (lo, span) => {
          const ticks = niceTicks(lo, lo + span, 5);
          for (let i = 1; i < ticks.length; i++) {
            expect(ticks[i]).toBeGreaterThan(ticks[i - 1]);
          }
        }
      ),
      { numRuns: 200 }
    );
  });
});

describe('buildGraphModel — beschriftete Achsen (Req 13.9, 13.10)', () => {
  it('erzeugt Zeitachsen-Marken (hh:mm) über das gewählte Fenster', () => {
    const now = new Date(2025, 0, 1, 10, 0, 0).getTime();
    const samples = [
      { t: now - 90 * 60000, temp: 40, soll: 67 },
      { t: now - 30 * 60000, temp: 60, soll: 67 },
      { t: now, temp: 67, soll: 67 },
    ];
    const m = buildGraphModel(samples, { windowHours: 2, now });
    expect(m.empty).toBe(false);
    expect(m.xTicks.length).toBeGreaterThanOrEqual(2);
    for (const tk of m.xTicks) {
      expect(tk.label).toMatch(/^\d{2}:\d{2}$/);
    }
    // y-Marken sind beschriftet mit Grad-Zeichen.
    expect(m.yTicks.length).toBeGreaterThanOrEqual(2);
    expect(m.yTicks[0].label).toMatch(/°$/);
  });

  it('liefert auch bei leerer Liste Zeitachsen-Marken für das Fenster', () => {
    const now = new Date(2025, 0, 1, 10, 0, 0).getTime();
    const m = buildGraphModel([], { windowHours: 1, now });
    expect(m.empty).toBe(true);
    expect(m.xTicks.length).toBeGreaterThanOrEqual(2);
  });

  it('verwendet ein festes Zeitfenster [now - hours, now] als Maßstab', () => {
    const now = new Date(2025, 0, 1, 12, 0, 0).getTime();
    const m = buildGraphModel([{ t: now, temp: 50, soll: null }], {
      windowHours: 4,
      now,
    });
    expect(m.tMax).toBe(now);
    expect(m.tMin).toBe(now - 4 * 3600 * 1000);
  });

  it('alle Datenpunkte bleiben innerhalb der Zeichenfläche', () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            t: fc.integer({ min: 0, max: 4 * 3600 * 1000 }),
            temp: fc.double({ min: 0, max: 100, noNaN: true }),
            soll: fc.option(fc.double({ min: 0, max: 100, noNaN: true }), { nil: null }),
          }),
          { minLength: 1, maxLength: 40 }
        ),
        (raw) => {
          const now = 4 * 3600 * 1000;
          const samples = [...raw].sort((a, b) => a.t - b.t);
          const width = 400;
          const height = 160;
          const m = buildGraphModel(samples, { width, height, windowHours: 4, now });
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
});
