import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import {
  isValidHysteresis,
  MIN_HYSTERESIS,
  MAX_HYSTERESIS,
} from '../../custom_components/brausteuerung/frontend/brausteuerung-logic.js';

// Feature: brausteuerung, Property 15: Validierung des Hysteresebandes

/**
 * Validates: Requirements 4.9, 4.10
 *
 * Property 15 (siehe design.md): Ein Eingabewert ist genau dann ein gültiges
 * Hystereseband, wenn er eine Zahl mit 0 < v <= 5 ist. Die Karte nutzt
 * `isValidHysteresis` nur noch für sofortiges Feedback im Einstellungs-Panel;
 * die Ableitung des wirksamen Wertes (früher `resolveHysteresis`) liegt seit
 * v3.0.0 in Python (tests/test_logic_properties.py).
 */

describe('Property 15: Validierung des Hysteresebandes', () => {
  // Teil 1: Gültige Werte (0 < v <= 5) werden akzeptiert.
  it('akzeptiert gültige Werte (0 < v <= 5)', () => {
    fc.assert(
      fc.property(
        fc.double({ min: 0, max: MAX_HYSTERESIS, noNaN: true }).filter((v) => v > 0 && v <= MAX_HYSTERESIS),
        (v) => {
          expect(isValidHysteresis(v)).toBe(true);
        }
      ),
      { numRuns: 100 }
    );
  });

  // Teil 2: Werte <= 0 oder > 5 sind ungültig.
  it('lehnt Werte <= 0 oder > 5 ab', () => {
    fc.assert(
      fc.property(
        fc.oneof(
          fc.double({ min: -1000, max: 0, noNaN: true }), // <= 0 (inkl. 0)
          fc.double({ min: MAX_HYSTERESIS + 0.0001, max: 1000, noNaN: true }) // > 5
        ),
        (v) => {
          // 0 selbst ist ungültig (Bereich ist > 0).
          expect(isValidHysteresis(v)).toBe(false);
        }
      ),
      { numRuns: 100 }
    );
  });

  // Teil 3: Nicht-numerische / fehlende Werte sind ungültig.
  it('behandelt nicht-numerische oder fehlende Werte als ungültig', () => {
    fc.assert(
      fc.property(
        fc.oneof(
          fc.constant(null),
          fc.constant(undefined),
          fc.constant('unknown'),
          fc.constant('unavailable'),
          fc.constant(''),
          fc.constant(Number.NaN),
          fc.string().filter((s) => Number.isNaN(Number(s)) || s.trim() === '')
        ),
        (v) => {
          expect(isValidHysteresis(typeof v === 'number' ? v : Number(v))).toBe(false);
          // Rohwerte ohne Zahlentyp sind nie gültig.
          if (typeof v !== 'number') expect(isValidHysteresis(v)).toBe(false);
        }
      ),
      { numRuns: 100 }
    );
  });

  // Teil 4: Exakte Bandgrenzen — untere Grenze 0 ungültig, obere Grenze 5 gültig.
  it('behandelt die Bandgrenzen korrekt: 0 ungültig, 5 gültig', () => {
    expect(isValidHysteresis(MIN_HYSTERESIS)).toBe(false); // 0 ist nicht > 0
    expect(isValidHysteresis(MAX_HYSTERESIS)).toBe(true); // 5 ist <= 5
  });
});
