// @vitest-environment jsdom
//
// Ladereihenfolge der Karte (Design §12.0, §13.2, Finding 1).
//
// Die Integration bindet die Karte per `add_extra_js_url` in index.html ein.
// Das Modul lädt also beim App-Start, bevor HA `ha-panel-lovelace` definiert
// hat. Der Modulkopf wartet deshalb mit `customElements.whenDefined` und lädt
// das Logikmodul dynamisch über `import.meta.url`. Diese Datei läuft in einer
// eigenen jsdom-Umgebung, in der `ha-panel-lovelace` anfangs NICHT existiert.

import { describe, it, expect, vi } from "vitest";

const CARD_PATH = "../../custom_components/brausteuerung/frontend/brausteuerung-card.js";

/** Minimale Fake-LitElement-Basis (Rendering wird hier nicht geprüft). */
class FakeLitElement extends HTMLElement {
  connectedCallback() {}
  disconnectedCallback() {}
  requestUpdate() {}
}
FakeLitElement.prototype.html = (strings, ...values) => ({ strings, values });
FakeLitElement.prototype.css = () => "";

/** Lässt laufende Microtasks und einen Makrotask-Durchlauf abarbeiten. */
const flush = () => new Promise((resolve) => setTimeout(resolve, 20));

describe("Ladereihenfolge der Karte (extra_module_url)", () => {
  it("wartet auf ha-panel-lovelace und definiert die Karte erst danach", async () => {
    vi.resetModules();
    expect(customElements.get("ha-panel-lovelace")).toBeUndefined();

    let rejected = null;
    let settled = false;
    const loading = import(CARD_PATH).then(
      () => {
        settled = true;
      },
      (err) => {
        rejected = err;
        settled = true;
      }
    );

    await flush();
    // Kein TypeError beim Laden vor dem Lovelace-Panel, Karte noch nicht da.
    expect(rejected).toBeNull();
    expect(settled).toBe(false);
    expect(customElements.get("brausteuerung-card")).toBeUndefined();

    customElements.define("ha-panel-lovelace", class extends FakeLitElement {});
    await loading;

    expect(rejected).toBeNull();
    expect(customElements.get("brausteuerung-card")).toBeDefined();
    expect(customElements.get("brausteuerung-card-editor")).toBeDefined();
    expect(window.customCards.some((c) => c.type === "brausteuerung-card")).toBe(true);
  });
});
