import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import { canStart } from '../../custom_components/brausteuerung/frontend/brausteuerung-logic.js';

// Feature: brausteuerung, Property 10: Start-Verfügbarkeit (canStart-Prädikat)

/**
 * Property 10: Start-Verfügbarkeit (canStart-Prädikat)
 * Validates: Requirements 8.1, 8.2
 *
 * Für jedes Rezept (auch das leere) und jede Sensor-Gültigkeit gilt:
 * Die Start-Aktion ist genau dann verfügbar, wenn das Rezept mindestens eine
 * Raststufe enthält UND ein gültiger Sensorwert vorliegt:
 *
 *   canStart(recipe, sensorValid) === (recipe.length >= 1 && sensorValid === true)
 *
 * Generator für eine Raststufe (gemäß design.md, Testing Strategy):
 *   name:        beliebiger String
 *   temperature: 0 ≤ t ≤ 100
 *   duration:    ganzzahlig 1 ≤ d ≤ 240
 */
const raststufe = fc.record({
  name: fc.string(),
  temperature: fc.double({ min: 0, max: 100, noNaN: true }),
  duration: fc.integer({ min: 1, max: 240 }),
});

describe('Property 10: Start-Verfügbarkeit (canStart-Prädikat)', () => {
  it('canStart(recipe, sensorValid) === (recipe.length >= 1 && sensorValid === true) für beliebige Rezepte (inkl. leer) und beide Booleans', () => {
    fc.assert(
      fc.property(
        // fc.array erzeugt sowohl leere als auch nicht-leere Rezepte.
        fc.array(raststufe),
        fc.boolean(),
        (recipe, sensorValid) => {
          const expected = recipe.length >= 1 && sensorValid === true;
          expect(canStart(recipe, sensorValid)).toBe(expected);
        }
      ),
      { numRuns: 100 }
    );
  });

  // Explizite Abdeckung aller vier Kombinationen (leer/nicht-leer × valid/invalid).
  it('deckt alle vier Kombinationen aus Rezeptzustand und Sensor-Gültigkeit ab', () => {
    const emptyRecipe = [];
    const nonEmptyRecipe = [{ name: 'Maischen', temperature: 67, duration: 60 }];

    // 1. nicht-leer + gültig  ⇒ Start verfügbar
    expect(canStart(nonEmptyRecipe, true)).toBe(true);

    // 2. nicht-leer + ungültig ⇒ Start deaktiviert
    expect(canStart(nonEmptyRecipe, false)).toBe(false);

    // 3. leer + gültig         ⇒ Start deaktiviert
    expect(canStart(emptyRecipe, true)).toBe(false);

    // 4. leer + ungültig       ⇒ Start deaktiviert
    expect(canStart(emptyRecipe, false)).toBe(false);
  });
});
