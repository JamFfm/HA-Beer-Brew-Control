import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import { parseLibrary } from '../../custom_components/brausteuerung/frontend/brausteuerung-logic.js';

// Feature: brausteuerung, Property 16: Bibliothek aus dem Benutzerspeicher robust parsen

/**
 * Validates: Requirements 12.8, 12.11
 *
 * Die Bibliotheksoperationen (Speichern, Ersetzen, Löschen) liegen seit v3.0.0
 * in Python (tests/test_logic_properties.py). Die Karte nutzt `parseLibrary`
 * nur noch zum Vorfiltern der alten user_data bei der einmaligen Migration.
 */

const raststufe = fc.record({
  name: fc.string(),
  temperature: fc.double({ min: 0, max: 100, noNaN: true }),
  duration: fc.integer({ min: 1, max: 240 }),
});

// Nicht-leerer (getrimmter) Rezeptname.
const recipeName = fc.string({ minLength: 1, maxLength: 20 }).filter((s) => s.trim() !== '');

const bibliotheksRezept = fc.record({
  name: recipeName,
  steps: fc.array(raststufe, { maxLength: 6 }),
});

/** Bibliothek mit eindeutigen (getrimmten) Namen erzeugen. */
const uniqueLibrary = fc.array(bibliotheksRezept, { maxLength: 8 }).map((arr) => {
  const seen = new Set();
  const out = [];
  for (const r of arr) {
    const key = r.name.trim();
    if (!seen.has(key)) {
      seen.add(key);
      out.push({ name: key, steps: r.steps });
    }
  }
  return out;
});

describe('Property 16: parseLibrary ist roundtrip-sicher und robust', () => {
  // Roundtrip: parseLibrary(JSON.stringify(lib)) tief gleich lib.
  it('parseLibrary(JSON.stringify(lib)) ist tief gleich lib', () => {
    fc.assert(
      fc.property(uniqueLibrary, (lib) => {
        expect(parseLibrary(JSON.stringify(lib))).toEqual(lib);
      }),
      { numRuns: 100 }
    );
  });

  // Bereits geparstes Array wird ebenso akzeptiert.
  it('parseLibrary(lib) als Array ist tief gleich lib', () => {
    fc.assert(
      fc.property(uniqueLibrary, (lib) => {
        expect(parseLibrary(lib)).toEqual(lib);
      }),
      { numRuns: 100 }
    );
  });

  // Beschädigter Rohwert => leere Bibliothek.
  it('parseLibrary liefert [] bei beschädigtem/kein-Array Rohwert', () => {
    fc.assert(
      fc.property(
        fc.oneof(
          fc.constant(null),
          fc.constant(undefined),
          fc.constant('{nicht json'),
          fc.constant('42'),
          fc.constant('"text"'),
          fc.integer(),
          fc.record({ foo: fc.string() })
        ),
        (raw) => {
          expect(parseLibrary(raw)).toEqual([]);
        }
      ),
      { numRuns: 50 }
    );
  });

  // Einträge ohne gültige Struktur werden verworfen.
  it('parseLibrary verwirft Einträge ohne name-String oder steps-Array', () => {
    const raw = [
      { name: 'Gut', steps: [] },
      { name: 42, steps: [] },
      { name: 'Ohne Schritte' },
      null,
      { name: 'Auch gut', steps: [{ name: 'a', temperature: 50, duration: 10 }] },
    ];
    expect(parseLibrary(raw).map((r) => r.name)).toEqual(['Gut', 'Auch gut']);
  });
});
