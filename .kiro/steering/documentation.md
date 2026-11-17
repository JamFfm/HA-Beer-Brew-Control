# Projektdokumentation und Konventionen

## Zweisprachige Dokumentation

- Die Projektdokumentation existiert zweisprachig: `README.md` ist englisch und
  der Standard (GitHub und HACS zeigen sie an), `README.de.md` ist die deutsche
  Fassung. Beide verlinken sich gegenseitig über den Sprachumschalter unter der
  Hauptüberschrift.
- Jede Änderung an Funktionen, Bedienung, Installation, Versionen,
  Sicherheitshinweisen oder CHANGELOG erfolgt in **beiden** Dateien
  inhaltsgleich.
- Struktur, Abschnittsnummern (1–16), Tabellen, Code-Blöcke, Befehle,
  Entity-IDs, Bilder, Badges und Links bleiben in beiden Dateien parallel.
  Interne Anker (`#...`) müssen auf die Überschriften der jeweiligen Sprache
  zeigen.
- UI-Texte der Karte werden in der Sprache der Datei zitiert, und zwar
  wörtlich aus `TRANSLATIONS.en` bzw. `TRANSLATIONS.de` in
  `custom_components/brausteuerung/frontend/brausteuerung-logic.js`.
  HA-Menüpfade in der Sprache der Datei (z. B. „Settings → Devices & services“
  bzw. „Einstellungen → Geräte & Dienste“).
- Die Sicherheitshinweise (unabhängiger Hardware-Übertemperaturschutz/STB,
  Aktor schaltet bei HA-Ausfall ab, nie unbeaufsichtigt) bleiben in beiden
  Sprachen gleich prominent.
- `tests/test_repo_consistency.py` prüft die Synchronität beider Dateien und
  muss grün sein.

## Projektkonventionen

- Versionsangaben einheitlich in `manifest.json`, `package.json`,
  `package-lock.json`, der Versionskonstante `VERSION` der Karte
  (`brausteuerung-card.js`), `README.md` und `README.de.md`.
- Entity-IDs (`sensor.brausteuerung_*` usw.) und die Domäne `brausteuerung`
  bleiben stabil.
- Änderungen an der Sicherheitslogik nur zusammen mit Tests.
