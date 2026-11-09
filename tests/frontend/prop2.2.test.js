// Feature: brausteuerung, Property 3: Eingabe-Validierung von Solltemperatur und Haltezeit
//
// Validates: Requirements 2.2, 2.3, 2.4
//
// Property 3: Für jede Kombination aus Solltemperatur `t` und Haltezeit `d` gilt:
// Die Raststufe ist genau dann gültig, wenn `t` eine Zahl mit 0 ≤ t ≤ 100 ist UND
// `d` eine Ganzzahl mit `d > 0` ist. Der Add-Flow selbst liegt seit v3.0.0 in
// Python (tests/test_logic_properties.py); die Karte nutzt isValidRaststufe nur
// noch für sofortiges Feedback.

import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import { isValidRaststufe } from '../../custom_components/brausteuerung/frontend/brausteuerung-logic.js';

/**
 * Referenz-Spezifikation der Gültigkeit, unabhängig von der Implementierung
 * formuliert (Req 2.2, 2.3): temp ist endliche Zahl mit 0 ≤ temp ≤ 100 UND
 * dur ist Ganzzahl > 0.
 */
function expectedValid(temp, dur) {
  const tempValid =
    typeof temp === 'number' &&
    Number.isFinite(temp) &&
    temp >= 0 &&
    temp <= 100;
  const durValid = Number.isInteger(dur) && dur > 0;
  return tempValid && durValid;
}

/**
 * Generator für Solltemperaturen: Mischung aus gültigen In-Range-Werten,
 * Out-of-Range-Werten, negativen Werten, NaN/Infinity und nicht-numerischen
 * Werten — deckt den Eingaberaum bewusst breit ab.
 */
const tempArb = fc.oneof(
  // gültiger Bereich 0..100
  fc.double({ min: 0, max: 100, noNaN: true }),
  // exakte Bandgrenzen
  fc.constantFrom(0, 100),
  // außerhalb des Bereichs (zu hoch / negativ)
  fc.double({ min: 100.0001, max: 1000, noNaN: true }),
  fc.double({ min: -1000, max: -0.0001, noNaN: true }),
  // Sonderwerte
  fc.constantFrom(NaN, Infinity, -Infinity),
  // nicht-numerische Werte
  fc.string(),
  fc.constantFrom(null, undefined)
);

/**
 * Generator für Haltezeiten: Mischung aus gültigen positiven Ganzzahlen,
 * 0, negativen Ganzzahlen, nicht-ganzzahligen (Dezimal-)Werten, NaN und
 * nicht-numerischen Werten.
 */
const durArb = fc.oneof(
  // gültig: Ganzzahl > 0
  fc.integer({ min: 1, max: 240 }),
  // 0 und negative Ganzzahlen (ungültig)
  fc.integer({ min: -240, max: 0 }),
  // nicht-ganzzahlige Dezimalwerte (ungültig)
  fc.double({ min: 0.0001, max: 240, noNaN: true }).filter((d) => !Number.isInteger(d)),
  // Sonderwerte
  fc.constantFrom(NaN, Infinity, -Infinity),
  // nicht-numerische Werte
  fc.string(),
  fc.constantFrom(null, undefined)
);

describe('Property 3: Eingabe-Validierung von Solltemperatur und Haltezeit', () => {
  it('isValidRaststufe ist genau dann true, wenn temp Zahl in [0,100] und dur Ganzzahl > 0', () => {
    fc.assert(
      fc.property(fc.string(), tempArb, durArb, (name, temp, dur) => {
        expect(isValidRaststufe(name, temp, dur)).toBe(expectedValid(temp, dur));
      }),
      { numRuns: 100 }
    );
  });
});
