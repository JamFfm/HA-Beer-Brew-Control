import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import { isSensorValid } from '../../custom_components/brausteuerung/frontend/brausteuerung-logic.js';

// Feature: brausteuerung, Property 12: Validierung des Sensorwerts

/**
 * Validates: Requirements 1.3
 *
 * Property 12 (siehe design.md): Für jeden Sensor-Rohwert (beliebiger String,
 * Zahl oder fehlender Wert) gilt: `isSensorValid` ist genau dann `true`, wenn
 * der Wert in eine endliche Zahl geparst werden kann; für `unknown`,
 * `unavailable`, leere/whitespace-only oder nicht-numerische Werte sowie
 * `null`/`undefined`/sonstige Typen ist er `false`.
 */

/**
 * Unabhängiges Orakel für die erwartete Gültigkeit eines Sensor-Rohwerts.
 * Diese Implementierung leitet sich direkt aus der Spezifikation ab und ist
 * absichtlich getrennt von der getesteten Funktion gehalten.
 *
 * @param {*} raw Beliebiger Sensor-Rohwert.
 * @returns {boolean} Erwartete Gültigkeit.
 */
function oracle(raw) {
  if (raw === null || raw === undefined) {
    return false;
  }
  if (typeof raw === 'number') {
    return Number.isFinite(raw);
  }
  if (typeof raw === 'string') {
    const trimmed = raw.trim();
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

// Numerische Strings: aus finiten Zahlen (float/integer) erzeugt.
const numericStringGen = fc.oneof(
  fc.float({ noNaN: true, noDefaultInfinity: true }).map((n) => String(n)),
  fc.integer().map((n) => String(n))
);

// Home-Assistant-Sonderzustände, inkl. abweichender Groß-/Kleinschreibung.
const haLiteralGen = fc.constantFrom('unknown', 'unavailable', 'UNKNOWN', 'Unavailable');

// Leerer String.
const emptyStringGen = fc.constant('');

// Nur-Whitespace-Strings.
const whitespaceGen = fc
  .array(fc.constantFrom(' ', '\t', '\n', '\r', '\f', '\v'), { minLength: 1, maxLength: 8 })
  .map((parts) => parts.join(''));

// Beliebige Strings (können numerisch oder nicht-numerisch sein) — deckt den
// gesamten String-Eingaberaum ab, damit auch Grenzfälle wie '  12.5  ',
// '1e3', '0x1f', 'NaN', 'Infinity' usw. auftreten.
const arbitraryStringGen = fc.string();

// Rohe Zahlen, inklusive Infinity/-Infinity/NaN.
const numberGen = fc.oneof(
  fc.float(),
  fc.integer(),
  fc.constantFrom(Infinity, -Infinity, NaN)
);

// Fehlende und sonstige Werte.
const otherGen = fc.constantFrom(null, undefined, true, false, {}, []);

// Gesamter gemischter Eingaberaum.
const rawValueGen = fc.oneof(
  numericStringGen,
  haLiteralGen,
  emptyStringGen,
  whitespaceGen,
  arbitraryStringGen,
  numberGen,
  otherGen
);

describe('Property 12: Validierung des Sensorwerts', () => {
  it('isSensorValid stimmt für jeden Rohwert mit dem unabhängigen Orakel überein', () => {
    fc.assert(
      fc.property(rawValueGen, (raw) => {
        expect(isSensorValid(raw)).toBe(oracle(raw));
      }),
      { numRuns: 300 }
    );
  });
});
