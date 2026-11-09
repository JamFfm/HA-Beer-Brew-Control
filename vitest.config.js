import { defineConfig } from "vitest/config";
export default defineConfig({
  test: {
    // Reine Logik- und Property-Tests laufen in Node ohne DOM.
    environment: "node",
    // Die JS-Tests liegen unter tests/frontend/ (die Python-Tests unter tests/
    // werden von pytest ausgeführt und passen nicht auf dieses Muster).
    include: ["tests/**/*.{test,spec}.{js,mjs}"],
    globals: true,
  },
});
