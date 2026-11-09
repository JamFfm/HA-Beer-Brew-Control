import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import { shouldUpdateDecision } from '../../custom_components/brausteuerung/frontend/brausteuerung-logic.js';

// Feature: brausteuerung, Property 11: Render-Stabilität während der Eingabe

/**
 * Property 11: Render-Stabilität während der Eingabe (Validates: Requirements 7.1, 7.2)
 *
 * Für jeden Komponentenzustand gilt: Ist das Settings-Panel geöffnet
 * (`showSettings`) ODER wird eine Rast editiert (`editIndex >= 0`), und
 * betrifft die Änderung ausschließlich `hass` (keine der Properties
 * `_showSettings`, `_editIndex`, `_localRecipe`), dann liefert
 * `shouldUpdateDecision` `false` (kein Re-Render). Ändert sich mindestens
 * eine der Properties `_showSettings`, `_editIndex` oder `_localRecipe`,
 * liefert sie `true`. Ist weder editiert noch das Settings-Panel offen
 * (`showSettings === false` UND `editIndex < 0`), liefert sie immer `true`.
 *
 * Generatoren:
 *   - showSettings: boolean
 *   - editIndex:    integer -1..10
 *   - changedProps: Set<string> aus einer Teilmenge bekannter Property-Namen
 *     (inkl. der reinen `hass`-Änderung).
 */

const RELEVANT_PROPS = ['_showSettings', '_editIndex', '_localRecipe'];
const ALL_PROPS = ['hass', '_showSettings', '_editIndex', '_localRecipe'];

/** Oracle gemäß design.md / shouldUpdate-Spezifikation. */
function oracle(showSettings, editIndex, changedProps) {
  if (showSettings || editIndex >= 0) {
    return RELEVANT_PROPS.some((p) => changedProps.has(p));
  }
  return true;
}

describe('Property 11: Render-Stabilität während der Eingabe', () => {
  it('shouldUpdateDecision entspricht dem Oracle für beliebige Zustände', () => {
    fc.assert(
      fc.property(
        fc.boolean(),
        fc.integer({ min: -1, max: 10 }),
        fc.subarray(ALL_PROPS),
        (showSettings, editIndex, propsSubset) => {
          const changedProps = new Set(propsSubset);
          const expected = oracle(showSettings, editIndex, changedProps);
          expect(
            shouldUpdateDecision(showSettings, editIndex, changedProps)
          ).toBe(expected);
        }
      ),
      { numRuns: 200 }
    );
  });

  it('editierend/Settings offen + nur "hass" geändert ⇒ false (kein Re-Render)', () => {
    fc.assert(
      fc.property(
        fc.boolean(),
        fc.integer({ min: -1, max: 10 }),
        (showSettings, editIndex) => {
          // Bedingung erzwingen: editierend ODER Settings offen.
          fc.pre(showSettings || editIndex >= 0);
          const changedProps = new Set(['hass']);
          expect(
            shouldUpdateDecision(showSettings, editIndex, changedProps)
          ).toBe(false);
        }
      ),
      { numRuns: 200 }
    );
  });

  it('editierend/Settings offen + relevante Property geändert ⇒ true', () => {
    fc.assert(
      fc.property(
        fc.boolean(),
        fc.integer({ min: -1, max: 10 }),
        fc.subarray(ALL_PROPS),
        fc.constantFrom(...RELEVANT_PROPS),
        (showSettings, editIndex, propsSubset, relevant) => {
          fc.pre(showSettings || editIndex >= 0);
          // Mindestens eine relevante Property ist enthalten.
          const changedProps = new Set([...propsSubset, relevant]);
          expect(
            shouldUpdateDecision(showSettings, editIndex, changedProps)
          ).toBe(true);
        }
      ),
      { numRuns: 200 }
    );
  });

  it('weder editierend noch Settings offen ⇒ immer true', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: -10, max: -1 }),
        fc.subarray(ALL_PROPS),
        (editIndex, propsSubset) => {
          const changedProps = new Set(propsSubset);
          expect(shouldUpdateDecision(false, editIndex, changedProps)).toBe(
            true
          );
        }
      ),
      { numRuns: 100 }
    );
  });
});
