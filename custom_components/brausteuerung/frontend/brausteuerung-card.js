/**
 * brausteuerung-card.js
 *
 * Custom Lovelace Card der Brausteuerung (LitElement-basiert), ausgeliefert von
 * der Integration `brausteuerung` (v3.0.1).
 *
 * Die Karte ist ein dünner Client: Regelung, Sicherheitsfunktionen, Rezept und
 * Bibliothek laufen serverseitig in der Python-Integration. Die Karte
 *   - abonniert den Zustand über `brausteuerung/subscribe` (Snapshot),
 *   - sendet Aktionen per `hass.callWS({type: "brausteuerung/<cmd>", …})`,
 *   - liest nur Ist-Temperatur und Heizungszustand direkt aus `hass.states`.
 * Optik, Struktur, CSS-Klassen und Texte sind gegenüber v2.6.6 unverändert.
 *
 * LitElement wird NICHT von einem externen CDN geladen, sondern aus dem von
 * Home Assistant registrierten `ha-panel-lovelace`-Element abgeleitet.
 *
 * @module brausteuerung-card
 */

// ---------------------------------------------------------------------------
// Version und Laden des Moduls (Design §12.0)
// ---------------------------------------------------------------------------
// Die Version muss mit manifest.json und package.json übereinstimmen (Tests).
// Der Cache-Buster ist die Query `?v=<Manifest-Version>`, mit der die
// Integration die Karte per `add_extra_js_url` einbindet; das Logikmodul wird
// mit genau derselben Query nachgeladen (keine handgepflegte Version im Import).
const VERSION = "3.0.1";

/**
 * URL des Logikmoduls zur (absoluten) URL der Karte: gleiches Verzeichnis,
 * gleiche Query (`?v=<Manifest-Version>`). Exportiert für Tests (AK 7).
 *
 * @param {string} cardUrl - absolute URL der Karte (z. B. `import.meta.url`)
 * @returns {string} absolute URL von `brausteuerung-logic.js` mit derselben Query
 */
export function logicUrlFor(cardUrl) {
  return new URL(`./brausteuerung-logic.js${new URL(cardUrl).search}`, cardUrl).href;
}

// Laden über extra_module_url beim App-Start: erst warten, bis HA das
// Lovelace-Panel registriert hat, dann LitElement daraus ableiten.
await customElements.whenDefined("ha-panel-lovelace");
const LitElement = Object.getPrototypeOf(customElements.get("ha-panel-lovelace"));
const html = LitElement.prototype.html;
const css = LitElement.prototype.css;
const LOGIC_URL = logicUrlFor(import.meta.url);
const {
  isValidRaststufe,
  isSensorValid,
  isValidHysteresis,
  displayRecipeName,
  parseLibrary,
  canStart,
  clampGraphHours,
  buildHistoryGraphConfig,
  translate,
  resolveLanguage,
  resolveUnit,
  TRANSLATIONS,
  DEFAULT_LANGUAGE,
  DEFAULT_GRAPH_HOURS,
  HYSTERESIS_BAND,
  Status,
} = await import(/* @vite-ignore */ LOGIC_URL);

// Präfix aller WebSocket-Befehle der Integration.
const WS_PREFIX = "brausteuerung/";

// Wartezeit bis zum (einzigen) erneuten Abo-Versuch nach Fehler oder
// `{loaded: false}` (Design §12.1).
const SUBSCRIBE_RETRY_MS = 10000;

// Keys im HA-Benutzerspeicher (frontend user_data).
// Alte Bibliothek und alter aktiver Name aus v2.x: werden nur noch einmalig
// gelesen und per `library/import` übernommen (Design §10.2), danach bleiben
// sie als Backup unverändert liegen.
const LEGACY_LIBRARY_KEY = "brausteuerung_recipes";
const LEGACY_ACTIVE_NAME_KEY = "brausteuerung_active_recipe";
// Merker: Übernahme der alten Bibliothek für diesen Benutzer erledigt.
const MIGRATED_KEY = "brausteuerung_recipes_migrated";

// Key der Graph-Einstellungen (gewählte Anzeigedauer) im HA-Benutzerspeicher (Req 13).
const GRAPH_SETTINGS_KEY = "brausteuerung_graph_settings";

class BrausteuerungCard extends LitElement {
  static get properties() {
    return {
      hass: { type: Object },
      _config: { type: Object },
      _showSettings: { type: Boolean },
      _editIndex: { type: Number },
      _localRecipe: { type: Array },
      _errorMessage: { type: String },
      _showLibrary: { type: Boolean },
      // Temperaturverlauf-Graph (Req 13): gewählte Anzeigedauer in Stunden.
      _graphHours: { type: Number },
      // Letzter Snapshot der Integration (Objekt) oder `null`, solange keiner
      // vorliegt bzw. die Integration nicht geladen ist (Design §12.1).
      _snapshot: { type: Object },
      _notLoaded: { type: Boolean },
    };
  }

  constructor() {
    super();
    this._config = {};
    this._showSettings = false;
    this._editIndex = -1;
    // Wird nur noch als Re-Render-Auslöser für Fehlermeldungen genutzt
    // (siehe `_setError`); das Rezept selbst kommt aus dem Snapshot.
    this._localRecipe = [];
    this._errorMessage = "";
    this._showLibrary = false;
    this._snapshot = null;
    this._notLoaded = false;
    // Abo-Verwaltung (Design §12.1): Promise aus `subscribeMessage`, der
    // einzige Retry-Timer und der Verbindungszustand des Elements.
    this._unsubPromise = null;
    this._retryTimer = null;
    this._connected = false;
    // Hinweis bei abweichender Server-Version (bleibt bis zum Neuladen stehen).
    this._versionMismatch = "";
    // Bibliotheksmigration höchstens einmal pro Karteninstanz anstoßen.
    this._migrationStarted = false;
    // Temperaturverlauf-Graph (Req 13): Die Card bettet die native
    // Home-Assistant-`history-graph`-Karte ein (echte Recorder-Historie).
    // `_graphHours` steuert `hours_to_show`; `_graphCard` ist das eingebettete
    // Karten-Element.
    this._graphHours = DEFAULT_GRAPH_HOURS;
    this._graphCard = null;
    this._graphCardSensor = null;
    this._graphCardSetpoint = null;
    this._graphCardHours = null;
    this._graphCardLang = null;
    this._graphCardUnit = null;
    this._graphCardLoading = false;
    // Gemessene Höhe der Hauptansicht in Pixeln (Req 17): dient als
    // `min-height` für die Panels „Einstellungen"/„Rezepte verwalten", damit
    // die Karte beim Umschalten nicht springt. Absichtlich KEINE reaktive
    // Property — ein Schreiben darf kein Re-Render auslösen (Render-Schleifen).
    this._mainViewHeight = 0;
  }

  /**
   * Übernimmt die Kartenkonfiguration. Seit v3.0.0 hat die Karte keine
   * eigenen Optionen mehr; alte Schlüssel wie `sensor_entity` werden ignoriert.
   * @param {Object} config Lovelace-Kartenkonfiguration.
   */
  setConfig(config) {
    this._config = config || {};
  }

  getCardSize() {
    return 5;
  }

  static getConfigElement() {
    return document.createElement("brausteuerung-card-editor");
  }

  static getStubConfig() {
    return {};
  }

  // =========================================================================
  // Lifecycle — Abo und Echtzeit-Countdown-Tick (Req 4.7)
  // =========================================================================

  /**
   * Startet beim Einhängen der Card den 1-Sekunden-Intervall-Tick und das
   * Snapshot-Abo (falls `hass` schon gesetzt ist).
   */
  connectedCallback() {
    super.connectedCallback();
    this._connected = true;
    this._startCountdownTick();
    // Gewählte Anzeigedauer (hours_to_show) aus dem HA-Benutzerspeicher laden (Req 13.3).
    this._loadGraphSettings();
    if (this.hass) this._subscribe();
  }

  /** Räumt Tick, Abo und Retry-Timer beim Aushängen der Card auf. */
  disconnectedCallback() {
    super.disconnectedCallback();
    this._connected = false;
    this._stopCountdownTick();
    this._clearRetryTimer();
    this._unsubscribe();
  }

  // =========================================================================
  // Snapshot-Abo (Design §12.1)
  // =========================================================================

  /**
   * Abonniert den Snapshot der Integration (idempotent). Die Bibliothek
   * home-assistant-js-websocket erneuert das Abo bei einem Reconnect selbst.
   */
  _subscribe() {
    if (this._unsubPromise) return;
    const conn = this.hass?.connection;
    if (!conn || typeof conn.subscribeMessage !== "function") return;
    let promise;
    try {
      promise = Promise.resolve(
        conn.subscribeMessage((msg) => this._onSnapshot(msg), {
          type: `${WS_PREFIX}subscribe`,
        })
      );
    } catch (err) {
      promise = Promise.reject(err);
    }
    this._unsubPromise = promise;
    promise.catch(() => {
      // Nur reagieren, wenn dies noch das aktuelle Abo ist.
      if (this._unsubPromise === promise) this._onSubscriptionLost();
    });
  }

  /**
   * Meldet das aktuelle Abo ab: Promise auflösen, `unsub()` aufrufen, Fehler
   * dabei ignorieren, Feld leeren.
   */
  _unsubscribe() {
    const promise = this._unsubPromise;
    this._unsubPromise = null;
    if (!promise) return;
    promise.then(
      (unsub) => {
        try {
          if (typeof unsub === "function") {
            const result = unsub();
            if (result && typeof result.catch === "function") result.catch(() => {});
          }
        } catch (err) {
          // Abmelden fehlgeschlagen (z. B. Verbindung weg) — ignorieren.
        }
      },
      () => {}
    );
  }

  /** Plant genau einen erneuten Abo-Versuch (kein doppelter Timer). */
  _scheduleRetry() {
    if (this._retryTimer || !this._connected) return;
    this._retryTimer = setTimeout(() => {
      this._retryTimer = null;
      this._subscribe();
    }, SUBSCRIBE_RETRY_MS);
  }

  /** Löscht einen geplanten Abo-Retry. */
  _clearRetryTimer() {
    if (this._retryTimer) {
      clearTimeout(this._retryTimer);
      this._retryTimer = null;
    }
  }

  /**
   * Abo fehlgeschlagen oder Integration entladen: Platzhalter anzeigen, Abo
   * abmelden und genau einen Retry planen.
   */
  _onSubscriptionLost() {
    this._snapshot = null;
    this._notLoaded = true;
    this._unsubscribe();
    this._scheduleRetry();
  }

  /**
   * Verarbeitet ein Abo-Event der Integration.
   * @param {Object} msg Snapshot (`loaded: true`) oder `{loaded: false}`.
   */
  _onSnapshot(msg) {
    if (!msg || typeof msg !== "object" || msg.loaded === false) {
      this._onSubscriptionLost();
      return;
    }
    this._snapshot = msg;
    this._notLoaded = false;
    this._versionMismatch =
      typeof msg.version === "string" && msg.version !== VERSION
        ? this._t("err_version_mismatch", { card: VERSION, server: msg.version })
        : "";
    this._migrateLibrary();
  }

  /**
   * Liefert die effektive Anzeigedauer (`hours_to_show`) des Graphen (Req 13).
   *
   * Läuft ein Brauvorgang, wird das Fenster auf den **Startzeitpunkt** begrenzt:
   * `hours_to_show` = Zeit seit dem Start (`started_at` aus dem Snapshot),
   * gedeckelt auf die vom Benutzer gewählte Dauer. Dadurch beginnen **beide**
   * Linien (Ist + Soll) beim Brauvorgang; ältere Werte liegen außerhalb des
   * Fensters und werden nicht angezeigt — ganz ohne Datenbank-Löschung.
   * Außerhalb eines laufenden Prozesses gilt die gewählte Dauer (1–4 h).
   *
   * @returns {number} Anzeigedauer in Stunden (fraktional möglich).
   */
  _effectiveGraphHours() {
    const selected = clampGraphHours(this._graphHours);
    if (this._status !== Status.RUNNING) {
      return selected;
    }
    const startedAt = this._snapshot?.started_at;
    if (!startedAt) {
      return selected;
    }
    const startMs = Date.parse(startedAt);
    if (!Number.isFinite(startMs)) {
      return selected;
    }
    const sinceH = (Date.now() - startMs) / 3600000;
    // Mindestfensterbreite (1 min), damit der Graph zu Beginn nicht entartet.
    const MIN_H = 1 / 60;
    return Math.min(selected, Math.max(sinceH, MIN_H));
  }

  /**
   * Synchronisiert die eingebettete `history-graph`-Karte mit der aktuellen
   * Konfiguration (Sensor, Sollwert-Entität, effektive Anzeigedauer) —
   * in-place über `setConfig`, damit kein Flackern durch Neuerzeugen entsteht
   * (Req 13). Aktualisiert nur, wenn sich Sensor/Sollwert-Entität geändert
   * haben ODER sich die effektive Dauer um mind. ~1 min verschoben hat
   * (drosselt das Nachführen des Fensters bei laufendem Brauvorgang).
   */
  _syncGraphCard() {
    if (!this._graphCard) return;
    const sensorChanged = this._configuredSensor !== this._graphCardSensor;
    const setpointChanged = this._setpointEntity !== this._graphCardSetpoint;
    const langChanged = this._lang !== this._graphCardLang;
    const unitChanged = this._unit !== this._graphCardUnit;
    const desiredHours = this._effectiveGraphHours();
    const hoursChanged =
      !Number.isFinite(this._graphCardHours) ||
      Math.abs(desiredHours - this._graphCardHours) >= 1 / 60;
    if (!sensorChanged && !setpointChanged && !langChanged && !unitChanged && !hoursChanged) {
      return;
    }
    try {
      this._graphCard.setConfig(this._graphCardConfig());
      if (this.hass) this._graphCard.hass = this.hass;
      this._graphCardSensor = this._configuredSensor;
      this._graphCardSetpoint = this._setpointEntity;
      this._graphCardHours = desiredHours;
      this._graphCardLang = this._lang;
      this._graphCardUnit = this._unit;
    } catch (err) {
      // setConfig nicht möglich — bestehende Karte beibehalten.
    }
  }

  /**
   * Startet das Abo beim ersten verfügbaren `hass` und hält die eingebettete
   * `history-graph`-Karte aktuell (Req 13).
   * @param {Map} changed Geänderte Properties (LitElement).
   */
  updated(changed) {
    // Höhe der Hauptansicht festhalten (Req 17): Solange kein Panel offen ist,
    // ist `.view` die Hauptansicht — ihre tatsächliche Höhe wird gemessen und
    // beim Öffnen eines Panels als `min-height` gesetzt. Gemessen wird nur im
    // Hauptansicht-Zustand, der Wert landet in einem nicht-reaktiven Feld;
    // dadurch entsteht kein zusätzliches `requestUpdate` und keine Schleife.
    this._measureMainViewHeight();
    // Beim ersten verfügbaren `hass` abonnieren bzw. Benutzerdaten nachladen:
    // In `connectedCallback` ist die WebSocket-Verbindung oft noch nicht da.
    if (changed.has("hass") && this.hass && this.hass.connection) {
      this._subscribe();
      this._loadGraphSettings();
    }
    if (!this._graphCard) return;
    if (changed.has("hass") && this.hass) {
      this._graphCard.hass = this.hass;
    }
    // Fenster an Sensor/effektive Dauer anpassen (begrenzt bei laufendem
    // Brauvorgang auf den Startzeitpunkt).
    this._syncGraphCard();
  }

  /**
   * Misst die Höhe der Hauptansicht und merkt sie für die Panels (Req 17).
   *
   * Warum Messen statt fester Pixelhöhe: Die Hauptansicht ist variabel hoch
   * (Anzahl der Rasten, Fehlermeldung, Countdown, Graph). Eine feste Höhe wäre
   * entweder zu klein (Abschneiden) oder zu groß (Leerraum). Der gemessene
   * Wert wird nur als `min-height` auf den Ansichtscontainer gelegt: Ist ein
   * Panel höher, darf die Karte wachsen (kein Abschneiden, Req 17.3).
   *
   * Schleifenfreiheit: Es wird nur gemessen, wenn kein Panel offen ist, und
   * das Ergebnis liegt in einem nicht-reaktiven Feld. Es wird also nie ein
   * Re-Render ausgelöst; der Wert wirkt beim nächsten regulären Render, das
   * ohnehin durch das Öffnen des Panels stattfindet.
   */
  _measureMainViewHeight() {
    if (this._showSettings || this._showLibrary) return;
    const view = this.renderRoot && this.renderRoot.querySelector
      ? this.renderRoot.querySelector(".view")
      : null;
    if (!view) return;
    const height = view.offsetHeight;
    if (height > 0) this._mainViewHeight = height;
  }

  /**
   * Startet den Sekunden-Tick (idempotent). Da `_remainingHoldSeconds` aus
   * `hold_ends_at - now` berechnet wird, genügt ein lokaler Re-Render pro
   * Sekunde, um den Countdown laufen zu lassen — ohne auf Snapshot-Updates zu
   * warten. Der Tick erzwingt ein Re-Render nur, wenn gerade eine Haltezeit
   * läuft und weder Settings-Panel offen noch eine Rast in Bearbeitung ist
   * (damit aktive Eingabefelder erhalten bleiben, Req 7.1/7.2).
   */
  _startCountdownTick() {
    if (this._countdownTimer) return;
    this._countdownTimer = setInterval(() => {
      if (this._showSettings || this._editIndex >= 0) return;
      if (this._remainingHoldSeconds !== null) {
        this.requestUpdate();
      }
    }, 1000);
  }

  /** Stoppt und verwirft den Sekunden-Tick. */
  _stopCountdownTick() {
    if (this._countdownTimer) {
      clearInterval(this._countdownTimer);
      this._countdownTimer = null;
    }
  }

  // =========================================================================
  // Temperaturverlauf-Graph (Req 13) — eingebettete native history-graph-Karte
  // =========================================================================

  /**
   * Liefert die Konfiguration der eingebetteten `history-graph`-Karte: Ist-
   * Temperatur (konfigurierter Sensor) und Solltemperatur (Sensor-Entität der
   * Integration, in Rot) über die effektive Anzeigedauer (bei laufendem
   * Brauvorgang auf den Startzeitpunkt begrenzt) (Req 13).
   * @returns {Object} history-graph-Kartenkonfiguration.
   */
  _graphCardConfig() {
    const unit = this._unit;
    return buildHistoryGraphConfig(
      this._configuredSensor,
      this._setpointEntity,
      this._effectiveGraphHours(),
      {
        actual: `${this._t("graph_actual")} (${unit})`,
        setpoint: `${this._t("graph_setpoint")} (${unit})`,
      }
    );
  }

  /**
   * Erzeugt die eingebettete native `history-graph`-Karte einmalig über
   * `window.loadCardHelpers()` (asynchron, idempotent). Die Karte liest die
   * echte Recorder-Historie und aktualisiert sich selbst; wir reichen nur das
   * `hass`-Objekt durch. Ohne konfigurierten Sensor wird nichts erzeugt.
   * @returns {Promise<void>}
   */
  async _ensureGraphCard() {
    if (this._graphCard || this._graphCardLoading) return;
    if (typeof window === "undefined" || typeof window.loadCardHelpers !== "function") {
      return;
    }
    if (!this.hass || !this._configuredSensor) return;
    this._graphCardLoading = true;
    try {
      const helpers = await window.loadCardHelpers();
      const el = helpers.createCardElement(this._graphCardConfig());
      el.hass = this.hass;
      this._graphCard = el;
      this._graphCardSensor = this._configuredSensor;
      this._graphCardSetpoint = this._setpointEntity;
      this._graphCardHours = this._effectiveGraphHours();
      this._graphCardLang = this._lang;
      this._graphCardUnit = this._unit;
    } catch (err) {
      // history-graph konnte nicht erstellt werden — Platzhalter bleibt sichtbar.
    } finally {
      this._graphCardLoading = false;
      this.requestUpdate();
    }
  }

  /**
   * Lädt die gewählte Anzeigedauer (`hours_to_show`) aus dem HA-Benutzerspeicher
   * (Req 13.3). Fehlende/ungültige Werte fallen auf den Default (2 h) zurück.
   * Idempotenter Schutz. Nach dem Laden wird die eingebettete Karte ggf. neu
   * aufgebaut, damit die Dauer greift.
   * @returns {Promise<void>}
   */
  async _loadGraphSettings() {
    if (this._graphSettingsLoaded) return;
    const conn = this.hass?.connection;
    if (!conn || typeof conn.sendMessagePromise !== "function") {
      return;
    }
    try {
      const result = await conn.sendMessagePromise({
        type: "frontend/get_user_data",
        key: GRAPH_SETTINGS_KEY,
      });
      const value = result?.value;
      const parsed = typeof value === "string" ? JSON.parse(value) : value;
      if (parsed && typeof parsed === "object") {
        this._graphHours = clampGraphHours(parsed.hours);
        this._syncGraphCard();
      }
      this._graphSettingsLoaded = true;
    } catch (err) {
      // Einstellungen nicht verfügbar: Defaults beibehalten.
    }
  }

  /**
   * Persistiert die gewählte Anzeigedauer im HA-Benutzerspeicher (Req 13.3).
   * @returns {Promise<void>}
   */
  async _persistGraphSettings() {
    const conn = this.hass?.connection;
    if (!conn || typeof conn.sendMessagePromise !== "function") {
      return;
    }
    try {
      await conn.sendMessagePromise({
        type: "frontend/set_user_data",
        key: GRAPH_SETTINGS_KEY,
        value: JSON.stringify({ hours: this._graphHours }),
      });
      this._graphSettingsLoaded = true;
    } catch (err) {
      // Persistenz fehlgeschlagen: Einstellung bleibt zumindest lokal aktiv.
    }
  }

  /**
   * Setzt die Anzeigedauer des Graphen in Stunden (1–4), persistiert sie und
   * aktualisiert die eingebettete Karte (Req 13.3).
   * @param {number|string} hours Gewünschte Anzeigedauer in Stunden.
   */
  _setGraphHours(hours) {
    this._graphHours = clampGraphHours(hours);
    this._syncGraphCard();
    this._persistGraphSettings();
  }

  // =========================================================================
  // Rezept-Bibliothek (Req 12) — serverseitig, Aktionen per WebSocket
  // =========================================================================

  /**
   * Übernimmt einmalig pro Benutzer die alte Bibliothek aus dem
   * HA-Benutzerspeicher (Design §10.2). Nur für Administratoren, weil
   * `library/import` admin-only ist. Die alten user_data bleiben als Backup
   * unverändert; nach Erfolg wird der Merker `brausteuerung_recipes_migrated`
   * gesetzt. Fehler werden still ignoriert (nächster Versuch beim Neuladen).
   * @returns {Promise<void>}
   */
  async _migrateLibrary() {
    if (this._migrationStarted) return;
    if (!this.hass?.user?.is_admin) return;
    const conn = this.hass.connection;
    if (!conn || typeof conn.sendMessagePromise !== "function") return;
    this._migrationStarted = true;
    const getUserData = (key) =>
      conn.sendMessagePromise({ type: "frontend/get_user_data", key });
    try {
      const flag = await getUserData(MIGRATED_KEY);
      if (flag?.value === true) return;
      const [libraryRaw, activeRaw] = await Promise.all([
        getUserData(LEGACY_LIBRARY_KEY),
        getUserData(LEGACY_ACTIVE_NAME_KEY),
      ]);
      const recipes = parseLibrary(libraryRaw?.value);
      const activeName = this._parseLegacyActiveName(activeRaw?.value);
      if (recipes.length > 0 || activeName !== "") {
        const payload = { recipes };
        if (activeName !== "") payload.active_recipe_name = activeName;
        const result = await this._ws("library/import", payload);
        if (result && Number(result.skipped) > 0) {
          this._setError(this._t("import_skipped", { n: result.skipped }));
        }
      }
      await conn.sendMessagePromise({
        type: "frontend/set_user_data",
        key: MIGRATED_KEY,
        value: true,
      });
    } catch (err) {
      // Übernahme nicht möglich: alte Daten bleiben unberührt.
    }
  }

  /**
   * Liest den alten aktiven Rezeptnamen (`{"name": …}` als JSON-String oder Objekt).
   * @param {unknown} value Rohwert aus dem Benutzerspeicher.
   * @returns {string} Getrimmter Name oder "".
   */
  _parseLegacyActiveName(value) {
    let parsed = value;
    if (typeof value === "string") {
      try {
        parsed = JSON.parse(value);
      } catch (err) {
        return "";
      }
    }
    return parsed && typeof parsed.name === "string" ? parsed.name.trim() : "";
  }

  /**
   * Lädt ein Bibliotheks-Rezept als aktives Rezept (Req 12.4, 12.9).
   * Während eines laufenden Prozesses gesperrt (Req 12.9).
   * @param {string} name Name des zu ladenden Rezepts.
   * @returns {Promise<boolean>} `true`, wenn geladen wurde.
   */
  async _loadRecipeFromLibrary(name) {
    if (this._status === Status.RUNNING) {
      return false;
    }
    try {
      await this._ws("library/load", { name });
      this._errorMessage = "";
      return true;
    } catch (err) {
      this._handleWsError(err);
      return false;
    }
  }

  /**
   * Löscht ein Rezept aus der Bibliothek (Req 12.6).
   * @param {string} name Name des zu löschenden Rezepts.
   * @returns {Promise<boolean>}
   */
  async _deleteRecipeFromLibrary(name) {
    try {
      await this._ws("library/delete", { name });
      this._errorMessage = "";
      return true;
    } catch (err) {
      this._handleWsError(err);
      return false;
    }
  }

  /** Schaltet die Sichtbarkeit des Rezeptverwaltungs-Panels um (Req 12.3). */
  _toggleLibrary() {
    this._showLibrary = !this._showLibrary;
    this._errorMessage = "";
  }

  // =========================================================================
  // Lesende Getter (keine Seiteneffekte) — Quelle ist der Snapshot
  // =========================================================================

  /** @returns {Object} Einstellungen aus dem Snapshot (oder `{}`). */
  get _settings() {
    const settings = this._snapshot?.settings;
    return settings && typeof settings === "object" ? settings : {};
  }

  /** @returns {Array} Aktuelles Braurezept aus dem Snapshot. */
  get _recipe() {
    const recipe = this._snapshot?.recipe;
    return Array.isArray(recipe) ? recipe : [];
  }

  /** @returns {Array} Rezept-Bibliothek aus dem Snapshot. */
  get _library() {
    const library = this._snapshot?.library;
    return Array.isArray(library) ? library : [];
  }

  /**
   * Gespeicherter Name des aktiven Rezepts (Req 16.1). Leerer String, wenn kein
   * Name gesetzt ist.
   * @returns {string} Rezeptname (getrimmt) oder "".
   */
  get _activeRecipeName() {
    const raw = this._snapshot?.recipe_name;
    return typeof raw === "string" ? raw.trim() : "";
  }

  /**
   * Anzuzeigender Rezeptname (Req 16.1, 16.2): der gespeicherte Name oder der
   * Platzhalter „Neues Rezept", wenn (noch) kein Rezept ausgewählt ist.
   * @returns {string} Anzeigename.
   */
  get _displayRecipeName() {
    return displayRecipeName(this._activeRecipeName, this._t("new_recipe"));
  }

  /** @returns {string} Aktueller Betriebsstatus (idle/running/paused/done). */
  get _status() {
    return this._snapshot?.status ?? Status.IDLE;
  }

  /**
   * Aktuelle Oberflächensprache (Req 14) aus den Einstellungen der Integration;
   * Default Englisch bei fehlendem/ungültigem Wert.
   * @returns {string} 'en' oder 'de'.
   */
  get _lang() {
    return resolveLanguage(this._settings.language);
  }

  /**
   * Übersetzt einen Schlüssel in die aktuelle Oberflächensprache (Req 14).
   * @param {string} key Übersetzungsschlüssel.
   * @param {Object<string,(string|number)>} [vars] Platzhalterwerte.
   * @returns {string} Übersetzter Text.
   */
  _t(key, vars) {
    return translate(this._lang, key, vars);
  }

  /**
   * Gewählte Temperatureinheit für die Beschriftungen (Req 15); Default `°C`.
   * Reine Anzeige, keine Umrechnung.
   * @returns {string} '°C' oder '°F'.
   */
  get _unit() {
    return resolveUnit(this._settings.unit);
  }

  /**
   * Setzt die Temperatureinheit der Integration (Req 15, `display/update`).
   * @param {string} unit '°C' oder '°F'.
   */
  async _setUnit(unit) {
    if (!this.hass) return;
    try {
      await this._ws("display/update", { unit: resolveUnit(unit) });
    } catch (err) {
      this._handleWsError(err);
    }
  }

  /**
   * Setzt die Oberflächensprache der Integration (Req 14, `display/update`).
   * Wirkt auf die Card und auf die Benachrichtigungstexte der Integration.
   * @param {string} lang Sprachcode ('en'/'de').
   */
  async _setLanguage(lang) {
    if (!this.hass) return;
    try {
      await this._ws("display/update", { language: resolveLanguage(lang) });
    } catch (err) {
      this._handleWsError(err);
    }
  }

  /** @returns {number} Index der aktuell aktiven Raststufe. */
  get _currentStep() {
    const index = this._snapshot?.step_index;
    return Number.isInteger(index) ? index : 0;
  }

  /** @returns {string} Entity-ID des Sensors aus den Einstellungen (oder ""). */
  get _configuredSensor() {
    const sensor = this._settings.sensor_entity;
    return typeof sensor === "string" ? sensor : "";
  }

  /** @returns {string} Entity-ID des Heizungs-Aktors aus den Einstellungen (oder ""). */
  get _configuredHeater() {
    const heater = this._settings.heater_entity;
    return typeof heater === "string" ? heater : "";
  }

  /** @returns {string|null} Entity-ID des Solltemperatur-Sensors der Integration. */
  get _setpointEntity() {
    const entity = this._snapshot?.entities?.setpoint;
    return typeof entity === "string" && entity !== "" ? entity : null;
  }

  /**
   * State-Objekt der konfigurierten Sensor-Entität (oder `null`).
   * @returns {Object|null} HA-State-Objekt des Sensors.
   */
  get _currentTemp() {
    const sensor = this._configuredSensor;
    if (!this.hass || !sensor) return null;
    return this.hass.states[sensor] ?? null;
  }

  /**
   * Prüft die Gültigkeit des aktuellen Sensorwerts (Req 1.3).
   * @returns {boolean} `true`, wenn der Sensorwert eine endliche Zahl ist.
   */
  get _isSensorValid() {
    return isSensorValid(this._currentTemp?.state);
  }

  /**
   * Wert der Sicherheitsabschaltung zur Anzeige (Req 9.3, A6): während eines
   * Brauvorgangs die Schwelle aus dem Snapshot, sonst der eingestellte
   * Sicherheits-Offset (Anzeige als Differenz „𝚫t").
   * @returns {number} Anzeigewert oder `NaN`, wenn keiner vorliegt.
   */
  get _safetyThreshold() {
    const value = this._snapshot?.safety_threshold ?? this._settings.safety_offset;
    return typeof value === "number" ? value : NaN;
  }

  /**
   * Aktuell geltendes Hystereseband in °C (Req 4.9, 4.10) aus den
   * Einstellungen; ohne gültigen Wert der Default 1,0 °C.
   * @returns {number} Hystereseband in °C.
   */
  get _hysteresis() {
    const value = this._settings.hysteresis;
    return isValidHysteresis(value) ? value : HYSTERESIS_BAND;
  }

  /**
   * State-Objekt des konfigurierten Heizungs-Aktors (oder `null`).
   * @returns {Object|null} HA-State-Objekt des Heizungs-Aktors.
   */
  get _heaterState() {
    const heater = this._configuredHeater;
    if (!this.hass || !heater) return null;
    return this.hass.states[heater] ?? null;
  }

  /**
   * Echtzeit-Zustand des Heizungs-Aktors (Req 5.6, 5.7).
   * @returns {boolean} `true`, wenn der Aktor eingeschaltet ist.
   */
  get _isHeaterOn() {
    return this._heaterState?.state === "on";
  }

  /**
   * Verbleibende Haltezeit der aktiven Raststufe in Sekunden (Req 4.7).
   *
   * Quelle ist `hold_ends_at` aus dem Snapshot (nur in der Haltephase
   * gesetzt). Die Restzeit wird relativ zur aktuellen Uhrzeit berechnet, sodass
   * die Anzeige über den eigenen Intervall-Tick sekundengenau herunterzählt.
   *
   * @returns {number|null} Verbleibende Sekunden (>= 0) oder `null`.
   */
  get _remainingHoldSeconds() {
    const holdEndsAt = this._snapshot?.hold_ends_at;
    if (!holdEndsAt) {
      return null;
    }
    const finishMs = Date.parse(holdEndsAt);
    if (Number.isNaN(finishMs)) {
      return null;
    }
    const remainingMs = finishMs - Date.now();
    return Math.max(0, Math.round(remainingMs / 1000));
  }

  /**
   * Text im Fehlerbereich: aktuelle Fehlermeldung, sonst ein Hinweis auf eine
   * abweichende Server-Version (Design §9).
   * @returns {string} Anzuzeigender Text oder "".
   */
  get _displayError() {
    return this._errorMessage || this._versionMismatch;
  }

  // =========================================================================
  // Render-Stabilität (Req 7.1, 7.2)
  // =========================================================================
  shouldUpdate(changedProps) {
    // Ist das Bibliotheks-Panel offen, sollen reine hass-Updates die aktive
    // Namenseingabe nicht zerstören; Änderungen an Panel-Status/Snapshot/
    // Fehlermeldung lösen jedoch ein Re-Render aus (Req 7, Req 12). Der
    // Snapshot ersetzt hier die frühere Bibliothek und den Rezeptnamen.
    if (this._showLibrary) {
      return (
        changedProps.has("_showLibrary") ||
        changedProps.has("_snapshot") ||
        changedProps.has("_localRecipe") ||
        changedProps.has("_showSettings") ||
        changedProps.has("_editIndex")
      );
    }
    // Geänderte Anzeigedauer soll den Verlauf neu aufbauen — außer es wird
    // gerade eine Rast editiert (Req 7).
    if (this._editIndex < 0 && changedProps.has("_graphHours")) {
      return true;
    }
    // Reine hass-/Snapshot-Updates nur während der Inline-Bearbeitung einer
    // Rast unterdrücken (damit aktive Eingabefelder erhalten bleiben, Req
    // 7.1/7.2). Ist NUR das Settings-Panel offen, weiterhin rendern, damit der
    // Heizungs-Schalter (Farbe/Flamme) live aktualisiert — die
    // Settings-Eingabefelder binden an stabile Getter und bleiben erhalten.
    if (this._editIndex >= 0) {
      return (
        changedProps.has("_showSettings") ||
        changedProps.has("_editIndex") ||
        changedProps.has("_localRecipe")
      );
    }
    return true;
  }

  // =========================================================================
  // WebSocket-Aktionen und Fehlerabbildung (Design §12.2)
  // =========================================================================

  /**
   * Sendet einen Befehl an die Integration.
   * @param {string} cmd Befehl ohne Präfix, z. B. `rest/add`.
   * @param {Object} [payload] Felder des Befehls.
   * @returns {Promise<*>} Ergebnis des Befehls.
   */
  _ws(cmd, payload = {}) {
    return this.hass.callWS({ type: `${WS_PREFIX}${cmd}`, ...payload });
  }

  /**
   * Bildet einen WebSocket-Fehler auf eine übersetzte Meldung ab:
   * `unauthorized` → `err_admin_required`, `invalid_format`/`invalid_*` →
   * kontextabhängiger Fallback, sonst `err_<code>` (falls vorhanden), sonst
   * `err_generic`.
   *
   * @param {{code?: string|number}} err Fehlerobjekt aus `callWS`.
   * @param {string} [fallbackKey] Schlüssel für Eingabefehler.
   */
  _handleWsError(err, fallbackKey) {
    const code = err && err.code !== undefined && err.code !== null ? String(err.code) : "";
    const table = TRANSLATIONS[DEFAULT_LANGUAGE];
    let message;
    if (code === "unauthorized") {
      message = this._t("err_admin_required");
    } else if (code.startsWith("invalid_") && fallbackKey) {
      message = this._t(fallbackKey);
    } else if (code !== "" && table[`err_${code}`] !== undefined) {
      message = this._t(`err_${code}`);
    } else {
      message = this._t("err_generic", { code: code || "?" });
    }
    this._setError(message);
  }

  /**
   * Setzt eine Fehlermeldung und erzwingt ein Re-Render — auch während eine
   * Rast editiert wird. `shouldUpdate` blockiert in diesem Modus reine
   * `hass`-/`_errorMessage`-Änderungen, lässt aber Änderungen an
   * `_localRecipe` durch. Durch das Erneuern der `_localRecipe`-Referenz wird
   * der Fehlerhinweis daher zuverlässig sichtbar.
   *
   * @param {string} message Anzuzeigende Fehlermeldung.
   */
  _setError(message) {
    this._errorMessage = message;
    this._localRecipe = this._localRecipe.slice();
  }

  // =========================================================================
  // Aktionen — Rezept-/Entitäts-UI
  // =========================================================================
  // Clientseitige Vorprüfung über isValidRaststufe(name, temp, dur) für
  // sofortiges Feedback (F26); die verbindliche Prüfung macht der Server.
  // Formularfelder werden erst nach erfolgreichem WS-Aufruf geleert bzw. der
  // Edit-Modus beendet.

  /** @returns {HTMLElement|null} Element aus dem Shadow-DOM (oder `null`). */
  _q(selector) {
    return this.renderRoot?.querySelector(selector) ?? null;
  }

  /**
   * Liest die Werte aus dem Eingabeformular für neue Rasten.
   * @returns {{name: string, temp: string, dur: string}} Rohwerte der Felder.
   */
  _readNewInputs() {
    return {
      name: this._q("#new-name")?.value ?? "",
      temp: this._q("#new-temp")?.value ?? "",
      dur: this._q("#new-dur")?.value ?? "",
    };
  }

  /**
   * Liest die Werte aus der Inline-Bearbeitungszeile.
   * @returns {{name: string, temp: string, dur: string}} Rohwerte der Felder.
   */
  _readEditInputs() {
    return {
      name: this._q("#edit-name")?.value ?? "",
      temp: this._q("#edit-temp")?.value ?? "",
      dur: this._q("#edit-dur")?.value ?? "",
    };
  }

  /**
   * Fügt eine neue Raststufe ans Ende des Rezepts an (Req 2.1, 2.4, 2.5).
   *
   * Ungültige Eingaben werden nicht gesendet; stattdessen wird ein
   * Fehlerhinweis angezeigt und die Felder bleiben erhalten. Einen leeren
   * Namen ersetzt der Server durch den Standardnamen.
   */
  async _addStep() {
    if (this._status === Status.RUNNING) {
      return;
    }
    const { name, temp, dur } = this._readNewInputs();
    const temperature = Number(temp);
    const duration = Number(dur);
    if (!isValidRaststufe(name, temperature, duration)) {
      this._setError(this._t("err_invalid_step_add"));
      return;
    }
    try {
      await this._ws("rest/add", { name, temperature, duration });
      this._errorMessage = "";
      this._clearNewInputs();
    } catch (err) {
      this._handleWsError(err, "err_invalid_step_add");
    }
  }

  /** Leert das Eingabeformular für neue Rasten. */
  _clearNewInputs() {
    const nameEl = this._q("#new-name");
    const tempEl = this._q("#new-temp");
    const durEl = this._q("#new-dur");
    if (nameEl) nameEl.value = "";
    if (tempEl) tempEl.value = "";
    if (durEl) durEl.value = "";
  }

  /**
   * Beginnt die Bearbeitung der Rast an Position `i` (Req 3.1).
   * @param {number} i Index der zu bearbeitenden Rast.
   */
  _beginEdit(i) {
    if (this._status === Status.RUNNING) {
      return;
    }
    this._errorMessage = "";
    this._editIndex = i;
  }

  /** Bricht die laufende Bearbeitung ab und verwirft die Änderungen (Req 3.4). */
  _cancelEdit() {
    this._editIndex = -1;
    this._errorMessage = "";
  }

  /**
   * Übernimmt die bearbeitete Rast `i`, wenn die Eingabe gültig ist
   * (Req 3.2, 3.4). Bei ungültiger Eingabe oder Serverfehler bleibt die
   * Bearbeitung offen und es wird ein Fehlerhinweis angezeigt.
   *
   * @param {number} i Index der bearbeiteten Rast.
   */
  async _saveEdit(i) {
    const { name, temp, dur } = this._readEditInputs();
    const temperature = Number(temp);
    const duration = Number(dur);
    if (!isValidRaststufe(name, temperature, duration)) {
      this._setError(this._t("err_invalid_step_edit"));
      return;
    }
    try {
      await this._ws("rest/update", { index: i, name, temperature, duration });
      this._errorMessage = "";
      this._editIndex = -1;
    } catch (err) {
      this._handleWsError(err, "err_invalid_step_edit");
    }
  }

  /**
   * Entfernt die Rast an Position `i` (Req 3.3).
   * @param {number} i Index der zu entfernenden Rast.
   */
  async _remove(i) {
    if (this._status === Status.RUNNING) {
      return;
    }
    try {
      await this._ws("rest/delete", { index: i });
      this._errorMessage = "";
    } catch (err) {
      this._handleWsError(err);
    }
  }

  /** Entfernt alle Rasten aus dem Rezept (Req 3.6). */
  async _clear() {
    if (this._status === Status.RUNNING) {
      return;
    }
    try {
      await this._ws("recipe/clear");
      this._errorMessage = "";
    } catch (err) {
      this._handleWsError(err);
    }
  }

  /**
   * Verschiebt die Rast an Position `i` nach oben oder unten (Req 3.7).
   * Während eines laufenden Prozesses gesperrt (Req 3.8); an den
   * Listengrenzen wird nichts gesendet.
   *
   * @param {number} i        Index der zu verschiebenden Rast.
   * @param {-1|1} direction  `-1` = nach oben, `+1` = nach unten.
   */
  async _moveStep(i, direction) {
    if (this._status === Status.RUNNING) {
      return;
    }
    const target = i + direction;
    if ((direction !== -1 && direction !== 1) || target < 0 || target >= this._recipe.length) {
      return;
    }
    try {
      await this._ws("rest/move", { index: i, direction });
      this._errorMessage = "";
    } catch (err) {
      this._handleWsError(err);
    }
  }

  /** Schaltet die Sichtbarkeit des Settings-Panels um (Req 5.1, 6.1). */
  _toggleSettings() {
    this._showSettings = !this._showSettings;
    this._errorMessage = "";
  }

  // =========================================================================
  // Prozesssteuerung — Start/Stop/Nächste Rast (Req 8.3–8.7)
  // =========================================================================

  /**
   * Startet den Brauprozess (Req 8.3). Wird nur gesendet, wenn `canStart`
   * erfüllt ist (Rezept ≥ 1 Rast UND gültiger Sensorwert — Req 8.1, 8.2).
   */
  async _start() {
    if (!this.hass) return;
    if (!canStart(this._recipe, this._isSensorValid)) {
      return;
    }
    this._errorMessage = "";
    try {
      await this._ws("control/start");
    } catch (err) {
      this._handleWsError(err);
    }
  }

  /**
   * Stoppt den Brauprozess (Req 8.4). Die Integration schaltet die Heizung
   * aus; Stop ist nie eingeschränkt.
   */
  async _stop() {
    if (!this.hass) return;
    this._errorMessage = "";
    try {
      await this._ws("control/stop");
    } catch (err) {
      this._handleWsError(err);
    }
  }

  /** Manueller Stufenwechsel (Req 8.5–8.7). */
  async _nextStep() {
    if (!this.hass) return;
    try {
      await this._ws("control/next_rest");
    } catch (err) {
      this._handleWsError(err);
    }
  }

  /**
   * Schaltet den konfigurierten Heizungs-Aktor manuell um (Req 5.8): ist er
   * aktuell aus, wird `switch.turn_on` aufgerufen, sonst `switch.turn_off`.
   *
   * Es gibt bewusst KEINEN Konflikt-Lock: Während eines laufenden Brauprozesses
   * darf die Regelung der Integration den manuell gesetzten Zustand wieder
   * überschreiben (Req 5.9). Bei Fehlschlag des Service-Aufrufs wird ein
   * Hinweis angezeigt; der reale Zustand wird weiter aus dem `hass`-State
   * gelesen, sodass keine Anzeige-Diskrepanz entsteht.
   */
  _toggleHeater() {
    if (!this.hass) return;
    const heater = this._configuredHeater;
    if (!heater) {
      this._setError(this._t("err_no_heater"));
      return;
    }
    const service = this._isHeaterOn ? "turn_off" : "turn_on";
    try {
      this.hass.callService("switch", service, { entity_id: heater });
    } catch (err) {
      this._setError(this._t("err_heater_toggle"));
    }
  }

  // =========================================================================
  // Einstellungen (Req 4.9, 5.2, 6.4) — nur Administratoren
  // =========================================================================

  /**
   * Speichert die Einstellungen aus dem Settings-Panel. Gesendet werden nur
   * geänderte Felder an `settings/update`. Die Hysterese wird vorher mit
   * `isValidHysteresis` geprüft. Ohne Änderung schließt das Panel sofort; nach
   * einem Fehler bleibt es mit Meldung offen.
   */
  async _saveSettings() {
    const sensor = (this._q("#settings-sensor")?.value ?? "").trim();
    const heater = (this._q("#settings-heater")?.value ?? "").trim();
    const hyst = this._q("#settings-hysteresis")?.value ?? "";
    const payload = {};
    if (hyst !== "") {
      const num = Number(hyst);
      if (!isValidHysteresis(num)) {
        this._setError(this._t("err_invalid_hysteresis"));
        return;
      }
      if (num !== this._settings.hysteresis) payload.hysteresis = num;
    }
    if (sensor !== this._configuredSensor) payload.sensor_entity = sensor;
    if (heater !== this._configuredHeater) payload.heater_entity = heater;
    if (Object.keys(payload).length === 0) {
      this._errorMessage = "";
      this._showSettings = false;
      return;
    }
    const onlyHysteresis = Object.keys(payload).length === 1 && "hysteresis" in payload;
    try {
      await this._ws("settings/update", payload);
      this._errorMessage = "";
      this._showSettings = false;
    } catch (err) {
      // Eingabefehler: nur Hysterese geändert → Hysterese-Text, sonst wurde
      // Sensor und/oder Heizung geändert → Entity-Text (Server bleibt maßgeblich).
      this._handleWsError(err, onlyHysteresis ? "err_invalid_hysteresis" : "err_invalid_entity");
    }
  }

  static get styles() {
    return css`
      :host {
        display: block;
      }
      ha-card {
        padding: 16px;
      }
      /* Kopfzeile: Titel links, Rezeptname mittig, Aktionen rechts (Req 16.1).
         Titel und Aktionen nehmen nur ihre natürliche Breite (auto), der
         flexible Rest geht an die mittlere Spalte mit dem Rezeptnamen
         (minmax(0, 1fr) erlaubt das Schrumpfen inkl. Ellipsis). Da beide
         Außenspalten gleich behandelt werden und der Name selbst zentriert
         ist, bleibt er optisch mittig. */
      .header {
        display: grid;
        grid-template-columns: auto minmax(0, 1fr) auto;
        align-items: center;
        gap: 8px;
      }
      /* Gemeinsame Schriftgröße für Kartentitel und Rezeptnamen (Req 16.1):
         explizit 1.5em (der Browser-Default von <h2>), damit der Name genauso
         groß ist wie „🍺 Brausteuerung" und der Titel unverändert aussieht. */
      .header h2,
      .recipe-name {
        font-size: 1.5em;
        line-height: 1.2;
      }
      .header h2 {
        margin: 0;
        min-width: 0;
        white-space: nowrap;
      }
      .recipe-name {
        font-weight: 700;
        text-align: center;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        color: var(--primary-text-color, #000);
      }
      .header-actions {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 4px;
      }
      .lang-select {
        padding: 2px 4px;
        border: 1px solid var(--divider-color, #ccc);
        border-radius: 4px;
        background: var(--card-background-color, #fff);
        color: var(--primary-text-color, #000);
        font-size: 0.8em;
        cursor: pointer;
      }
      .placeholder {
        color: var(--secondary-text-color);
        font-size: 0.9em;
        padding: 8px 0;
      }
      .error {
        margin-top: 8px;
        padding: 8px 12px;
        border-radius: 4px;
        background: var(--error-color, #e74c3c);
        color: white;
        font-size: 0.9em;
      }

      /* Status-/Temperaturblock (Task 8.3) */
      .status-block {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        margin: 8px 0 4px;
      }
      .status-temp {
        display: flex;
        flex-direction: column;
        min-width: 0;
      }
      .temp-value {
        font-size: 2em;
        font-weight: 600;
        line-height: 1.1;
        color: var(--primary-text-color, #000);
      }
      .temp-hint {
        font-size: 1.05em;
        font-weight: 500;
        color: var(--warning-color, #ff9800);
      }
      .temp-sensor-id {
        font-size: 0.8em;
        color: var(--secondary-text-color);
      }
      .status-badge {
        flex-shrink: 0;
        padding: 2px 10px;
        border-radius: 12px;
        font-size: 0.8em;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.03em;
        background: var(--secondary-background-color, #f0f0f0);
        color: var(--secondary-text-color);
      }
      .status-right {
        display: flex;
        align-items: center;
        gap: 8px;
        flex-shrink: 0;
      }
      .heater-toggle {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 2px 10px;
        border-radius: 12px;
        font-size: 0.8em;
        font-weight: 600;
        cursor: pointer;
        border: 1px solid var(--divider-color, #ccc);
        background: var(--secondary-background-color, #f0f0f0);
        color: var(--secondary-text-color);
      }
      .heater-toggle.on {
        background: var(--error-color, #e74c3c);
        color: #fff;
        border-color: var(--error-color, #e74c3c);
      }
      .heater-toggle .flame {
        filter: none;
      }
      .status-countdown {
        font-size: 0.95em;
        font-weight: 600;
        color: var(--primary-color, #03a9f4);
        margin-bottom: 8px;
      }
      .status-badge.status-running {
        background: var(--primary-color, #03a9f4);
        color: var(--text-primary-color, #fff);
      }
      .status-badge.status-paused {
        background: var(--warning-color, #ff9800);
        color: #fff;
      }
      .status-badge.status-done {
        background: var(--success-color, #4caf50);
        color: #fff;
      }
      .status-threshold {
        font-size: 0.85em;
        color: var(--secondary-text-color);
        margin-bottom: 8px;
      }

      /* Buttons */
      .btn {
        padding: 6px 12px;
        border: 1px solid var(--divider-color, #ccc);
        border-radius: 4px;
        background: var(--card-background-color, #fff);
        color: var(--primary-text-color, #000);
        cursor: pointer;
        font-size: 0.9em;
      }
      .btn:hover {
        background: var(--secondary-background-color, #f0f0f0);
      }
      .btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .btn-primary {
        background: var(--primary-color, #03a9f4);
        color: var(--text-primary-color, #fff);
        border-color: var(--primary-color, #03a9f4);
      }
      .btn-danger {
        background: var(--error-color, #e74c3c);
        color: #fff;
        border-color: var(--error-color, #e74c3c);
      }
      .icon-btn {
        border: none;
        background: none;
        cursor: pointer;
        font-size: 1.1em;
        padding: 2px 6px;
        color: var(--primary-text-color, #000);
      }
      .icon-btn:disabled {
        opacity: 0.4;
        cursor: not-allowed;
      }

      /* Recipe list */
      .steps {
        list-style: none;
        margin: 8px 0;
        padding: 0;
      }
      .step {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 8px;
        border-radius: 4px;
        border: 1px solid var(--divider-color, #e0e0e0);
        margin-bottom: 6px;
      }
      .step .marker {
        width: 1.2em;
        text-align: center;
        font-weight: bold;
      }
      .step-info {
        display: flex;
        flex-direction: column;
        flex: 1;
        min-width: 0;
      }
      .step-name {
        font-weight: 500;
      }
      .step-detail {
        font-size: 0.85em;
        color: var(--secondary-text-color);
      }
      .step-actions {
        display: flex;
        gap: 2px;
      }
      /* Aktiv / abgeschlossen / verbleibend (Req 4.6) */
      .step.active {
        border-color: var(--primary-color, #03a9f4);
        background: var(--primary-color, #03a9f4);
        color: var(--text-primary-color, #fff);
      }
      .step.active .step-detail {
        color: var(--text-primary-color, #fff);
      }
      .step.done {
        opacity: 0.6;
      }
      .step.done .step-name {
        text-decoration: line-through;
      }
      .step.remaining {
        opacity: 0.95;
      }
      .step.editing {
        flex-direction: column;
        align-items: stretch;
      }
      .edit-row,
      .add-form {
        display: flex;
        gap: 6px;
        align-items: center;
      }
      .add-form {
        margin-top: 8px;
      }
      .edit-actions,
      .settings-actions,
      .recipe-actions {
        display: flex;
        gap: 6px;
        margin-top: 8px;
      }
      .recipe-actions {
        justify-content: flex-end;
      }
      .controls {
        display: flex;
        gap: 8px;
        margin-top: 12px;
      }
      .controls .btn {
        flex: 1;
        font-size: 1em;
        padding: 10px 12px;
      }
      input[type="text"],
      input[type="number"] {
        padding: 6px 8px;
        border: 1px solid var(--divider-color, #ccc);
        border-radius: 4px;
        box-sizing: border-box;
        background: var(--card-background-color, #fff);
        color: var(--primary-text-color, #000);
      }
      .add-form input[type="text"],
      .edit-row input[type="text"] {
        flex: 2;
        min-width: 0;
      }
      .add-form input[type="number"],
      .edit-row input[type="number"] {
        flex: 1;
        min-width: 0;
        width: 4em;
      }

      /* Settings panel */
      .settings h3 {
        margin: 8px 0;
      }
      .field-label {
        display: block;
        font-size: 0.85em;
        color: var(--secondary-text-color);
        margin-top: 8px;
      }
      .field {
        width: 100%;
        margin-top: 4px;
        box-sizing: border-box;
        border: 1px solid var(--divider-color, #ccc);
        border-radius: 4px;
        padding: 6px 8px;
        background: var(--card-background-color, #fff);
        color: var(--primary-text-color, #000);
      }

      /* Temperaturverlauf-Graph (Req 13) */
      .graph {
        margin-top: 12px;
        padding-top: 8px;
        border-top: 1px solid var(--divider-color, #e0e0e0);
      }
      .graph-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 6px;
      }
      .graph-title {
        font-weight: 500;
        font-size: 0.95em;
      }
      .graph-hours-label {
        font-size: 0.85em;
        color: var(--secondary-text-color);
      }
      .graph-hours {
        margin-left: 4px;
        padding: 2px 6px;
        border: 1px solid var(--divider-color, #ccc);
        border-radius: 4px;
        background: var(--card-background-color, #fff);
        color: var(--primary-text-color, #000);
      }
      .graph-svg {
        width: 100%;
        height: auto;
        aspect-ratio: 400 / 160;
        display: block;
        overflow: visible;
        background: var(--secondary-background-color, #fafafa);
        border-radius: 4px;
      }
      .graph-temp {
        stroke: var(--primary-color, #03a9f4);
        stroke-width: 2;
      }
      .graph-soll {
        stroke: var(--error-color, #e74c3c);
        stroke-width: 2;
        stroke-dasharray: 4 3;
      }
      .graph-axis {
        fill: var(--secondary-text-color, #888);
        color: var(--secondary-text-color, #888);
        font-size: 11px;
      }
      .graph-axis-title {
        fill: var(--secondary-text-color, #888);
        color: var(--secondary-text-color, #888);
        font-size: 11px;
        font-weight: 600;
      }
      .graph-axis-line {
        stroke: var(--divider-color, #ccc);
        stroke-width: 1;
      }
      .graph-grid {
        stroke: var(--divider-color, #e0e0e0);
        stroke-width: 0.5;
        opacity: 0.6;
      }
      .graph-legend {
        display: flex;
        gap: 16px;
        margin-top: 4px;
        font-size: 0.8em;
        color: var(--secondary-text-color);
      }
      .legend-item {
        display: inline-flex;
        align-items: center;
        gap: 4px;
      }
      .swatch {
        display: inline-block;
        width: 14px;
        height: 3px;
        border-radius: 2px;
      }
      .swatch.temp {
        background: var(--primary-color, #03a9f4);
      }
      .swatch.soll {
        background: var(--error-color, #e74c3c);
      }
      .graph-toggle {
        display: flex;
        align-items: center;
        gap: 6px;
        margin-top: 8px;
        font-size: 0.85em;
        color: var(--secondary-text-color);
      }
      /*
        Gemeinsamer Container für Hauptansicht und Panels (Req 17). Die
        gemessene Höhe der Hauptansicht wird als Inline-min-height gesetzt;
        box-sizing: border-box hält die Messung (offsetHeight) und die
        gesetzte Höhe deckungsgleich. Kein overflow: hidden, damit höhere
        Panel-Inhalte sichtbar bleiben (Req 17.3).
      */
      .view {
        box-sizing: border-box;
      }
    `;
  }

  render() {
    const recipe = this._recipe;
    const running = this._status === Status.RUNNING;
    const active = this._currentStep;
    const showStatusMarkers =
      this._status === Status.RUNNING || this._status === Status.PAUSED;
    // Offenes Panel = „Einstellungen" oder „Rezepte verwalten" (Req 17).
    const panelOpen = this._showSettings || this._showLibrary;

    return html`
      <ha-card>
        <div class="header">
          <h2>${this._t("app_title")}</h2>
          <!-- Name des aktiven Rezepts: oben mittig, fett (Req 16.1, 16.2) -->
          <div
            class="recipe-name"
            title=${`${this._t("active_recipe_tt")}: ${this._displayRecipeName}`}
          >
            ${this._displayRecipeName}
          </div>
          <div class="header-actions">
            <select
              class="lang-select"
              title=${this._t("language_tt")}
              @change=${(e) => this._setLanguage(e.target.value)}
            >
              <option value="en" ?selected=${this._lang === "en"}>EN</option>
              <option value="de" ?selected=${this._lang === "de"}>DE</option>
            </select>
            <button
              class="icon-btn"
              title=${this._t("manage_recipes_tt")}
              @click=${() => this._toggleLibrary()}
            >
              📋
            </button>
            <button
              class="icon-btn"
              title=${this._t("settings_tt")}
              @click=${() => this._toggleSettings()}
            >
              ⚙️
            </button>
          </div>
        </div>

        ${this._renderStatusBlock()}

        <!--
          Gemeinsamer Ansichtscontainer (Req 17): Hauptansicht und Panels
          liegen im selben Container. Bei offenem Panel erhält er die zuvor
          gemessene Höhe der Hauptansicht als min-height, damit die Karte beim
          Umschalten nicht springt. min-height statt height, damit höhere
          Panel-Inhalte nicht abgeschnitten werden (Req 17.3).
        -->
        <div
          class="view"
          style=${panelOpen && this._mainViewHeight > 0
            ? `min-height:${this._mainViewHeight}px`
            : ""}
        >
          ${!this._snapshot
            ? html`<div class="placeholder">${this._t("err_not_loaded")}</div>`
            : this._showLibrary
            ? this._renderLibrary(running)
            : this._showSettings
            ? this._renderSettings()
            : this._renderRecipe(recipe, running, active, showStatusMarkers)}

          ${panelOpen || !this._snapshot ? "" : this._renderControls(recipe, running)}

          ${panelOpen || !this._snapshot ? "" : this._renderGraph()}
        </div>

        ${this._displayError
          ? html`<div class="error">${this._displayError}</div>`
          : ""}
      </ha-card>
    `;
  }

  /**
   * Render des Rezeptverwaltungs-Panels (Req 12): Liste der Bibliotheks-Rezepte
   * mit „Laden"/„Löschen" je Eintrag sowie ein Namensfeld mit „Speichern unter…"
   * für das aktive Rezept. Während eines laufenden Prozesses sind verändernde
   * Aktionen, die das aktive Rezept ersetzen würden, deaktiviert (Req 12.9).
   *
   * @param {boolean} running Ob ein Prozess läuft.
   * @returns {import("lit").TemplateResult}
   */
  _renderLibrary(running) {
    const library = Array.isArray(this._library) ? this._library : [];
    return html`
      <div class="settings">
        <h3>${this._t("manage_recipes_title")}</h3>

        ${library.length === 0
          ? html`<div class="placeholder">${this._t("no_saved_recipes")}</div>`
          : html`
              <ul class="steps">
                ${library.map(
                  (r) => html`
                    <li class="step">
                      <span class="step-info">
                        <span class="step-name">${r.name}</span>
                        <span class="step-detail">${this._t("rests_count", { n: (r.steps || []).length })}</span>
                      </span>
                      <span class="step-actions">
                        <button
                          class="btn btn-primary btn-s"
                          title=${this._t("load_recipe_tt")}
                          ?disabled=${running}
                          @click=${() => this._loadRecipeFromLibrary(r.name)}
                        >
                          ${this._t("load")}
                        </button>
                        <button
                          class="btn btn-danger btn-s"
                          title=${this._t("delete_from_library_tt")}
                          @click=${() => this._deleteRecipeFromLibrary(r.name)}
                        >
                          🗑
                        </button>
                      </span>
                    </li>
                  `
                )}
              </ul>
            `}

        <label class="field-label" for="library-name">
          ${this._t("save_current_as")}
        </label>
        <input
          id="library-name"
          class="field"
          type="text"
          placeholder=${this._t("recipe_name_placeholder")}
        />
        <div class="settings-actions">
          <button
            class="btn btn-primary"
            ?disabled=${running}
            @click=${() => this._onSaveRecipeClick()}
          >
            ${this._t("save_as")}
          </button>
          <button class="btn" @click=${() => this._toggleLibrary()}>
            ${this._t("close")}
          </button>
        </div>
      </div>
    `;
  }

  /**
   * Liest den Namen aus dem Eingabefeld und speichert das aktive Rezept in der
   * Bibliothek (`library/save`). Existiert der Name bereits (`name_exists`),
   * wird vor dem Überschreiben eine Bestätigung eingeholt und erneut mit
   * `overwrite: true` gesendet (Req 12.1, 12.2).
   */
  async _onSaveRecipeClick() {
    const nameEl = this._q("#library-name");
    const name = (nameEl ? nameEl.value : "").trim();
    if (name === "") {
      this._setError(this._t("err_enter_name"));
      return;
    }
    try {
      await this._ws("library/save", { name });
    } catch (err) {
      if (err?.code !== "name_exists") {
        this._handleWsError(err, "err_invalid_name");
        return;
      }
      const ok = typeof window !== "undefined" && typeof window.confirm === "function"
        ? window.confirm(this._t("overwrite_confirm", { name }))
        : true;
      if (!ok) return;
      try {
        await this._ws("library/save", { name, overwrite: true });
        this._errorMessage = "";
      } catch (err2) {
        this._handleWsError(err2, "err_invalid_name");
      }
      return;
    }
    this._errorMessage = "";
    if (nameEl) nameEl.value = "";
  }

  /**
   * Render des Status-/Temperaturblocks (Task 8.3): Ist-Temperatur der
   * konfigurierten Sensor-Entität (Req 1.1), Hinweise bei fehlendem Sensor
   * (Req 1.4) bzw. ungültigem Sensorwert (Req 1.3), ein kleiner Status-Badge
   * (idle/running/paused/done — Prozessvisibilität, Req 4.6) sowie der
   * Sicherheitsschwellwert `soll + offset` (Req 9.3).
   *
   * Logik der Temperaturanzeige:
   *   - kein Sensor konfiguriert       → „⚠️ Kein Sensor gesetzt" (Req 1.4)
   *   - Sensor konfiguriert, ungültig  → „⚠️ Kein gültiger Sensorwert" (Req 1.3)
   *   - gültiger Sensorwert            → „{state} {unit}" (Req 1.1)
   *
   * @returns {import("lit").TemplateResult}
   */
  _renderStatusBlock() {
    const status = this._status;
    const threshold = this._safetyThreshold;

    let tempContent;
    if (!this._configuredSensor) {
      // Req 1.4: keine Sensor-Entität ausgewählt.
      tempContent = html`
        <span class="temp-hint">${this._t("no_sensor")}</span>
      `;
    } else if (!this._isSensorValid) {
      // Req 1.3: Sensor liefert unknown/unavailable/nicht-numerisch.
      tempContent = html`
        <span class="temp-hint">${this._t("invalid_sensor")}</span>
        <span class="temp-sensor-id">${this._configuredSensor}</span>
      `;
    } else {
      // Req 1.1: gültige Ist-Temperatur anzeigen. Einheit ist die gewählte
      // Anzeigeeinheit (Req 15 — reine Beschriftung, keine Umrechnung).
      const temp = this._currentTemp;
      tempContent = html`
        <span class="temp-value">${temp.state} ${this._unit}</span>
      `;
    }

    return html`
      <div class="status-block">
        <div class="status-temp">${tempContent}</div>
        <div class="status-right">
          ${this._renderHeaterState()}
          <span class="status-badge status-${status}">${this._t("status_" + status)}</span>
        </div>
      </div>
      <div class="status-threshold">
        ${Number.isFinite(threshold)
          ? this._t("safety_shutoff", { v: threshold, unit: this._unit })
          : this._t("safety_shutoff_none")}
      </div>
      <div class="status-threshold">
        ${this._t("hysteresis_status", { v: this._hysteresis, unit: this._unit })}
      </div>
      ${this._renderCountdown()}
    `;
  }

  /**
   * Render des Echtzeit-Zustands des Heizungs-Aktors neben der Ist-Temperatur
   * (Req 5.6) inkl. farbigem Flammensymbol, wenn der Aktor eingeschaltet ist
   * (Req 5.7), sowie eines Schalt-Buttons für das manuelle Umschalten (Req 5.8).
   *
   * Ist kein Heizungs-Aktor konfiguriert, wird nichts angezeigt.
   *
   * @returns {import("lit").TemplateResult|string}
   */
  _renderHeaterState() {
    if (!this._configuredHeater) {
      return "";
    }
    const on = this._isHeaterOn;
    return html`
      <button
        class="heater-toggle ${on ? "on" : "off"}"
        title=${on ? this._t("heater_turn_off_tt") : this._t("heater_turn_on_tt")}
        @click=${() => this._toggleHeater()}
      >
        ${on
          ? html`<span class="flame">🔥</span> ${this._t("heater_on")}`
          : html`${this._t("heater_off")}`}
      </button>
    `;
  }

  /**
   * Render des sekundengenauen Echtzeit-Countdowns der verbleibenden Haltezeit
   * (Req 4.7). Wird nur angezeigt, wenn ein Raststufen-Timer aktiv läuft.
   *
   * @returns {import("lit").TemplateResult|string}
   */
  _renderCountdown() {
    const remaining = this._remainingHoldSeconds;
    if (remaining === null) {
      return "";
    }
    const mm = Math.floor(remaining / 60);
    const ss = remaining % 60;
    const formatted = `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
    return html`
      <div class="status-countdown">${this._t("remaining_hold", { v: formatted })}</div>
    `;
  }

  /**
   * Render des Temperaturverlauf-Graphen (Req 13.1, 13.2). Zeichnet die
   * aufgezeichnete Ist-Temperatur und — sofern vorhanden — die Solltemperatur
   * in einer anderen Farbe als SVG-Polyline. Darunter befinden sich die
   * Bedienelemente zur Wahl der Anzeigedauer (1–4 h, Req 13.3) und zur
   * Daueranzeige der aktuellen Temperatur ohne aktiven Brauvorgang (Req 13.4).
   *
   * @returns {import("lit").TemplateResult}
   */
  _renderGraph() {
    // Native history-graph-Karte einbetten (echte Recorder-Historie). Beim
    // ersten Aufruf asynchron erzeugen; bis dahin einen Platzhalter zeigen.
    this._ensureGraphCard();

    const hoursSelector = html`
      <div class="graph-header">
        <span class="graph-title">${this._t("graph_title")}</span>
        <label class="graph-hours-label">
          ${this._t("duration")}
          <select
            class="graph-hours"
            @change=${(e) => this._setGraphHours(e.target.value)}
          >
            ${[1, 2, 3, 4].map(
              (h) => html`<option value=${h} ?selected=${this._graphHours === h}>${h} h</option>`
            )}
          </select>
        </label>
      </div>
    `;

    let body;
    if (!this._configuredSensor) {
      body = html`<div class="placeholder">${this._t("select_sensor_first")}</div>`;
    } else if (this._graphCard) {
      // Das eingebettete Karten-Element direkt rendern (Lit akzeptiert DOM-Nodes).
      body = this._graphCard;
    } else {
      body = html`<div class="placeholder">${this._t("loading_graph")}</div>`;
    }

    return html`
      <div class="graph">
        ${hoursSelector}
        ${body}
      </div>
    `;
  }

  /**
   * Render der Prozesssteuerung (Task 8.4): Start-/Stop-/„Nächste Rast"-Buttons
   * abhängig vom Status und der Start-Verfügbarkeit (`canStart`).
   *
   * - Status `running` → „⏹ Stop" und „⏭ Nächste Rast" (Req 8.4, 8.5).
   * - sonst            → „▶ Start"; deaktiviert, solange `canStart` nicht
   *                      erfüllt ist (Rezept ≥ 1 Rast UND gültiger Sensorwert —
   *                      Req 8.1, 8.2, 8.3).
   *
   * @param {Array} recipe   Aktuelles Rezept.
   * @param {boolean} running Ob ein Prozess läuft.
   * @returns {import("lit").TemplateResult}
   */
  _renderControls(recipe, running) {
    if (running) {
      return html`
        <div class="controls">
          <button class="btn btn-primary" @click=${() => this._nextStep()}>
            ${this._t("next_rest")}
          </button>
          <button class="btn btn-danger" @click=${() => this._stop()}>
            ${this._t("stop")}
          </button>
        </div>
      `;
    }

    const startDisabled = !canStart(recipe, this._isSensorValid);
    return html`
      <div class="controls">
        <button
          class="btn btn-primary"
          ?disabled=${startDisabled}
          title=${startDisabled ? this._t("start_disabled_tt") : this._t("start_tt")}
          @click=${() => this._start()}
        >
          ${this._t("start")}
        </button>
      </div>
    `;
  }

  /**
   * Render der Rezeptansicht: Raststufenliste mit Bearbeiten-/Löschen-Aktionen
   * sowie das Eingabeformular für neue Rasten (Req 3.1, 3.5, 4.6).
   *
   * @param {Array} recipe            Aktuelles Rezept.
   * @param {boolean} running         Ob ein Prozess läuft (Aktionen deaktiviert).
   * @param {number} active           Index der aktiven Rast.
   * @param {boolean} showMarkers     Ob Aktiv-/Fertig-Markierungen anzuzeigen sind.
   * @returns {import("lit").TemplateResult}
   */
  _renderRecipe(recipe, running, active, showMarkers) {
    return html`
      <div class="recipe">
        ${recipe.length === 0
          ? html`<div class="placeholder">${this._t("no_rests")}</div>`
          : html`
              <ol class="steps">
                ${recipe.map((step, i) => this._renderStep(step, i, running, active, showMarkers, recipe.length))}
              </ol>
            `}
      </div>

      ${this._renderAddForm(running)}

      ${recipe.length > 0
        ? html`
            <div class="recipe-actions">
              <button
                class="btn btn-danger"
                ?disabled=${running}
                @click=${() => this._clear()}
              >
                ${this._t("clear_all")}
              </button>
            </div>
          `
        : ""}
    `;
  }

  /**
   * Render einer einzelnen Raststufe — entweder als Anzeigezeile oder, wenn
   * `_editIndex === i`, als Inline-Bearbeitungszeile (Req 3.1, 3.2, 4.6).
   *
   * @param {Object} step          Die Raststufe.
   * @param {number} i             Index der Rast.
   * @param {boolean} running      Ob ein Prozess läuft.
   * @param {number} active        Index der aktiven Rast.
   * @param {boolean} showMarkers  Ob Aktiv-/Fertig-Markierungen anzuzeigen sind.
   * @param {number} total         Gesamtzahl der Rasten (für Verschiebe-Grenzen).
   * @returns {import("lit").TemplateResult}
   */
  _renderStep(step, i, running, active, showMarkers, total) {
    if (this._editIndex === i) {
      return html`
        <li class="step editing">
          <div class="edit-row">
            <input
              id="edit-name"
              type="text"
              placeholder=${this._t("name_optional")}
              .value=${step.name ?? ""}
            />
            <input
              id="edit-temp"
              type="number"
              min="0"
              max="100"
              step="0.5"
              placeholder=${this._unit}
              .value=${String(step.temperature ?? "")}
            />
            <input
              id="edit-dur"
              type="number"
              min="1"
              step="1"
              placeholder="min"
              .value=${String(step.duration ?? "")}
            />
          </div>
          <div class="edit-actions">
            <button class="btn btn-primary" @click=${() => this._saveEdit(i)}>
              ${this._t("save")}
            </button>
            <button class="btn" @click=${() => this._cancelEdit()}>
              ${this._t("cancel")}
            </button>
          </div>
        </li>
      `;
    }

    // Aktiv-/Fertig-/Verbleibend-Markierung (Req 4.6).
    let stateClass = "";
    let marker = "";
    if (showMarkers) {
      if (i < active) {
        stateClass = "done";
        marker = "✓";
      } else if (i === active) {
        stateClass = "active";
        marker = "▶";
      } else {
        stateClass = "remaining";
      }
    }

    return html`
      <li class="step ${stateClass}">
        <span class="marker">${marker}</span>
        <span class="step-info">
          <span class="step-name">${step.name}</span>
          <span class="step-detail">${this._t("step_detail", { temp: step.temperature, dur: step.duration, unit: this._unit })}</span>
        </span>
        <span class="step-actions">
          <button
            class="icon-btn"
            title=${this._t("move_up_tt")}
            ?disabled=${running || i === 0}
            @click=${() => this._moveStep(i, -1)}
          >
            ▲
          </button>
          <button
            class="icon-btn"
            title=${this._t("move_down_tt")}
            ?disabled=${running || i === total - 1}
            @click=${() => this._moveStep(i, 1)}
          >
            ▼
          </button>
          <button
            class="icon-btn"
            title=${this._t("edit_tt")}
            ?disabled=${running}
            @click=${() => this._beginEdit(i)}
          >
            ✎
          </button>
          <button
            class="icon-btn"
            title=${this._t("delete_tt")}
            ?disabled=${running}
            @click=${() => this._remove(i)}
          >
            🗑
          </button>
        </span>
      </li>
    `;
  }

  /**
   * Render des Eingabeformulars für neue Rasten (Req 2.1). Während eines
   * laufenden Prozesses sind die Felder und der Hinzufügen-Button deaktiviert
   * (Req 3.5).
   *
   * @param {boolean} running Ob ein Prozess läuft.
   * @returns {import("lit").TemplateResult}
   */
  _renderAddForm(running) {
    return html`
      <div class="add-form">
        <input
          id="new-name"
          type="text"
          placeholder=${this._t("name_optional")}
          ?disabled=${running}
        />
        <input
          id="new-temp"
          type="number"
          min="0"
          max="100"
          step="0.5"
          placeholder=${this._unit}
          ?disabled=${running}
        />
        <input
          id="new-dur"
          type="number"
          min="1"
          step="1"
          placeholder="min"
          ?disabled=${running}
        />
        <button
          class="btn btn-primary"
          ?disabled=${running}
          @click=${() => this._addStep()}
        >
          ${this._t("add_rest")}
        </button>
      </div>
    `;
  }

  /**
   * Render des Settings-Panels mit `<datalist>`-Autovervollständigung für
   * Sensor (`sensor.*`) und Heizung (`switch.*`) (Req 5.1, 6.1, 6.3, 7.3).
   *
   * Es werden bewusst `<input list=...>`-Felder mit `<datalist>` verwendet,
   * damit die Auswahlliste sich bei Auswahl nicht schließt und Eingaben nicht
   * verworfen werden. Die Felder lösen KEIN `requestUpdate` aus; `shouldUpdate`
   * unterdrückt zudem Re-Renders durch reine `hass`-Änderungen, solange das
   * Panel offen ist — die aktiven Eingabefelder bleiben dadurch erhalten.
   *
   * @returns {import("lit").TemplateResult}
   */
  _renderSettings() {
    const states = this.hass?.states ?? {};
    const entityIds = Object.keys(states);
    const sensorIds = entityIds.filter((id) => id.startsWith("sensor."));
    const switchIds = entityIds.filter((id) => id.startsWith("switch."));

    return html`
      <div class="settings">
        <h3>${this._t("settings_tt")}</h3>

        <label class="field-label" for="settings-sensor">${this._t("sensor_label")}</label>
        <input
          id="settings-sensor"
          class="field"
          type="text"
          list="sensor-options"
          placeholder="sensor.brau_temperatur"
          .value=${this._configuredSensor}
        />
        <datalist id="sensor-options">
          ${sensorIds.map((id) => html`<option value=${id}></option>`)}
        </datalist>

        <label class="field-label" for="settings-heater">${this._t("heater_label")}</label>
        <input
          id="settings-heater"
          class="field"
          type="text"
          list="switch-options"
          placeholder="switch.brau_heizung"
          .value=${this._configuredHeater}
        />
        <datalist id="switch-options">
          ${switchIds.map((id) => html`<option value=${id}></option>`)}
        </datalist>

        <label class="field-label" for="settings-hysteresis">
          ${this._t("hysteresis_label", { unit: this._unit })}
        </label>
        <input
          id="settings-hysteresis"
          class="field"
          type="number"
          min="0.1"
          max="5"
          step="0.1"
          placeholder="1.0"
          .value=${String(this._hysteresis)}
        />

        <label class="field-label" for="settings-unit">${this._t("unit_label")}</label>
        <select
          id="settings-unit"
          class="field"
          @change=${(e) => this._setUnit(e.target.value)}
        >
          <option value="°C" ?selected=${this._unit === "°C"}>°C</option>
          <option value="°F" ?selected=${this._unit === "°F"}>°F</option>
        </select>

        <div class="settings-actions">
          <button class="btn btn-primary" @click=${() => this._saveSettings()}>
            ${this._t("save")}
          </button>
          <button class="btn" @click=${() => this._toggleSettings()}>
            ${this._t("close")}
          </button>
        </div>
      </div>
    `;
  }
}

// ============================================================================
// Card Editor: Die Karte hat keine eigenen Optionen mehr (v3.0.0). Der Editor
// zeigt nur einen Hinweis, wo die Integration konfiguriert wird.
// ============================================================================
class BrausteuerungCardEditor extends LitElement {
  static get properties() {
    return { hass: { type: Object }, _config: { type: Object } };
  }

  setConfig(config) {
    this._config = config || {};
  }

  /** @returns {string} Oberflächensprache des HA-Benutzers (Default en). */
  get _lang() {
    return resolveLanguage(this.hass?.locale?.language ?? this.hass?.language);
  }

  render() {
    return html`
      <div style="padding:8px;">
        <p style="font-size:0.9em;color:var(--secondary-text-color);">
          ${translate(this._lang, "editor_note")}
        </p>
      </div>
    `;
  }
}

// ============================================================================
// Registrierung (idempotent — verhindert "already been used"-DOMException,
// falls das Modul mehrfach ausgewertet wird). Ist die Karte schon definiert,
// stammt sie meist aus einer alten Lovelace-Ressource von v2.x.
// ============================================================================
if (customElements.get("brausteuerung-card")) {
  console.warn(
    "brausteuerung-card ist bereits definiert – vermutlich über die alte Lovelace-Ressource " +
      "/local/brausteuerung-card.js. Bitte diese Ressource entfernen und die Seite neu laden."
  );
} else {
  customElements.define("brausteuerung-card", BrausteuerungCard);
}
if (!customElements.get("brausteuerung-card-editor")) {
  customElements.define("brausteuerung-card-editor", BrausteuerungCardEditor);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "brausteuerung-card")) {
  window.customCards.push({
    type: "brausteuerung-card",
    name: `HA-Beer-Brew-Control (v${VERSION})`,
    description:
      "HA-Beer-Brew-Control: brew/mash control for homebrewers — Maischesteuerung für Hobbybrauer (EN/DE)",
  });
}

// Versionshinweis in der Browser-Konsole (hilft beim Verifizieren nach Updates).
console.info(`%c brausteuerung-card %c v${VERSION} `,
  "color:white;background:#03a9f4;font-weight:700",
  "color:#03a9f4;background:white;font-weight:700");
