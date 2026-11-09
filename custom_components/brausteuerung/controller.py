"""BrauController: Zustandsautomat, Regelung, Watchdogs, Aktorpfad (Design §7).

Ein Controller pro Config Entry. Laufzeitzustand liegt nur im Speicher (S2).
Grundregeln:
- Entscheidungen fallen synchron im Event-Loop (Callbacks), nur Aktorbefehle sind
  Tasks und laufen ausschließlich über ``_spawn_actuator_task`` (Entry-Task, nie
  Background-Task).
- Abo-/Timer-Handles hält nur der Controller; abgemeldet wird nur über
  ``_async_cancel``. Über ``entry.async_on_unload`` läuft allein
  ``_async_remove_stop_hooks``.
- Jede Fremdausnahme endet im Fail-safe (Heizung aus, bei running → paused).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Concatenate

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Context,
    Event,
    EventStateChangedData,
    HassJob,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import ServiceNotFound, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from . import logic, messages, migration
from .const import (
    ACTUATOR_MAX_RETRIES,
    ACTUATOR_UNAVAILABLE_TIMEOUT,
    ACTUATOR_VERIFY_DELAY,
    COMM_LOSS_TIMEOUT,
    CONF_HEATER_ENTITY,
    CONF_HYSTERESIS,
    CONF_LANGUAGE,
    CONF_SAFETY_OFFSET,
    CONF_SENSOR_ENTITY,
    CONF_UNIT,
    CONTROL_INTERVAL,
    DEFAULT_HYSTERESIS,
    DEFAULT_LANGUAGE,
    DEFAULT_SAFETY_OFFSET,
    DEFAULT_UNIT,
    DOMAIN,
    FORCED_OFF_REASONS,
    INITIAL_OFF_RETRY_INTERVAL,
    MAX_LIBRARY_RECIPES,
    MODIFIED_SUFFIX,
    NOTIFICATION_ID_PREFIX,
    PHASE_HEATING,
    PHASE_HOLDING,
    SERVICE_CALL_TIMEOUT,
    SHUTDOWN_OFF_TIMEOUT,
    SHUTDOWN_TASK_TIMEOUT,
    STALE_SENSOR_TIMEOUT,
    STATUS_DONE,
    STATUS_IDLE,
    STATUS_PAUSED,
    STATUS_RUNNING,
)
from .logic import LibraryRecipe, LogicError, Rest

if TYPE_CHECKING:
    from .storage import BrauStore, StoredData

_LOGGER = logging.getLogger(__name__)

# Handles, die async_shutdown abmeldet. Die beiden HA-Stop-Hooks gehören
# ausdrücklich nicht dazu (sonst fiele Stage 2 nach Stage 1 weg).
_CANCEL_ON_SHUTDOWN = (
    "_sensor_unsub",
    "_heater_unsub",
    "_tick_unsub",
    "_at_started_unsub",
    "_hold_unsub",
    "_comm_loss_unsub",
    "_verify_off_unsub",
    "_actuator_unavailable_unsub",
)

_UNAVAILABLE_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)

# Einstellungen mit Defaults (entry.options).
_OPTION_DEFAULTS: dict[str, Any] = {
    CONF_SENSOR_ENTITY: None,
    CONF_HEATER_ENTITY: None,
    CONF_HYSTERESIS: DEFAULT_HYSTERESIS,
    CONF_SAFETY_OFFSET: DEFAULT_SAFETY_OFFSET,
    CONF_LANGUAGE: DEFAULT_LANGUAGE,
    CONF_UNIT: DEFAULT_UNIT,
}


class BrauError(ServiceValidationError):
    """Fachlicher Fehler; ``translation_key`` = Schlüssel unter ``exceptions``."""

    def __init__(self, code: str) -> None:
        super().__init__(translation_domain=DOMAIN, translation_key=code)
        self.code = code


@contextlib.contextmanager
def _logic_errors() -> Iterator[None]:
    """Übersetzt ``LogicError`` in ``BrauError`` mit gleichem Code."""
    try:
        yield
    except LogicError as err:
        raise BrauError(err.code) from err


def _guard[**P](
    func: Callable[Concatenate[BrauController, P], None],
) -> Callable[Concatenate[BrauController, P], None]:
    """Fail-safe-Hülle für synchrone Callbacks (Design §7.2, Schicht 2)."""

    @functools.wraps(func)
    def wrapper(self: BrauController, *args: P.args, **kwargs: P.kwargs) -> None:
        try:
            func(self, *args, **kwargs)
        except Exception:
            _LOGGER.exception("Interner Fehler in der Brausteuerung (%s)", func.__name__)
            self._fail_safe("internal_error")

    return wrapper


def _public_op[**P, R](
    func: Callable[Concatenate[BrauController, P], Awaitable[R]],
) -> Callable[Concatenate[BrauController, P], Coroutine[Any, Any, R]]:
    """Fail-safe-Hülle für öffentliche Operationen (Design §7.2, Schicht 3)."""

    @functools.wraps(func)
    async def wrapper(self: BrauController, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(self, *args, **kwargs)
        except BrauError:
            raise
        except Exception as err:
            _LOGGER.exception("Interner Fehler in der Brausteuerung (%s)", func.__name__)
            self._fail_safe("internal_error")
            raise BrauError("internal_error") from err

    return wrapper


class BrauController:
    """Regelt einen Maischvorgang für genau einen Config Entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        store: BrauStore,
        data: StoredData,
        version: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._store = store
        self._data = data
        self._version = version
        self._options: dict[str, Any] = {**_OPTION_DEFAULTS, **entry.options}
        self._data_lock = asyncio.Lock()
        self._listeners: list[CALLBACK_TYPE] = []

        # Laufzeitzustand (§4.3)
        self._status = STATUS_IDLE
        self._phase: str | None = None
        self._step_index = 0
        self._run_recipe: tuple[Rest, ...] = ()
        self._setpoint: float | None = None
        self._hold_ends_at: datetime | None = None
        self._started_at: datetime | None = None
        self._sensor_invalid_since: datetime | None = None
        self._overtemp_active = False
        self._safety_state_active = False
        self._desired_heater_on = False
        self._last_off_sent_at: datetime | None = None
        self._off_retry_count = 0
        self._retry_exhausted_logged = False
        self._pending_actuator_tasks: set[asyncio.Task[Any]] = set()
        self._shut_down = False
        self._starting = False
        self._initial_off_pending = True
        self._initial_off_inflight = False
        # Anzahl laufender OFF-Aufrufe an die aktuelle Heizung.
        self._off_inflight = 0
        self._initial_off_error_logged = False
        self._initial_off_failed_at: datetime | None = None

        # Handles (nur hier gehalten, Abmelden nur über _async_cancel)
        self._sensor_unsub: CALLBACK_TYPE | None = None
        self._heater_unsub: CALLBACK_TYPE | None = None
        self._tick_unsub: CALLBACK_TYPE | None = None
        self._at_started_unsub: CALLBACK_TYPE | None = None
        self._hold_unsub: CALLBACK_TYPE | None = None
        self._comm_loss_unsub: CALLBACK_TYPE | None = None
        self._verify_off_unsub: CALLBACK_TYPE | None = None
        self._actuator_unavailable_unsub: CALLBACK_TYPE | None = None
        self._stop_unsub: CALLBACK_TYPE | None = None
        self._shutdown_job_unsub: CALLBACK_TYPE | None = None

    # ------------------------------------------------------------------
    # Lesende Eigenschaften
    # ------------------------------------------------------------------

    @property
    def status(self) -> str:
        """Ablaufstatus (idle/running/paused/done)."""
        return self._status

    @property
    def phase(self) -> str | None:
        """Phase der aktiven Rast (heating/holding/None)."""
        return self._phase

    @property
    def step_index(self) -> int:
        """Index der aktiven Rast."""
        return self._step_index

    @property
    def setpoint(self) -> float | None:
        """Aktuelle Solltemperatur."""
        return self._setpoint

    @property
    def hold_ends_at(self) -> datetime | None:
        """Ende der Haltezeit (UTC)."""
        return self._hold_ends_at

    @property
    def started_at(self) -> datetime | None:
        """Startzeitpunkt des Brauvorgangs (UTC)."""
        return self._started_at

    @property
    def version(self) -> str:
        """Version der Integration (manifest.json)."""
        return self._version

    @property
    def run_recipe(self) -> tuple[Rest, ...]:
        """Beim Start übernommenes Rezept des laufenden/pausierten Vorgangs."""
        return self._run_recipe

    @property
    def recipe(self) -> tuple[Rest, ...]:
        """Aktives Rezept (übernommener Stand)."""
        return self._data.recipe

    @property
    def recipe_name(self) -> str:
        """Name des aktiven Rezepts."""
        return self._data.recipe_name

    @property
    def library(self) -> tuple[LibraryRecipe, ...]:
        """Rezeptbibliothek."""
        return self._data.library

    @property
    def options(self) -> dict[str, Any]:
        """Aktive Einstellungen samt Defaults."""
        return dict(self._options)

    @property
    def sensor_entity(self) -> str | None:
        """Konfigurierter Temperatursensor."""
        return self._options.get(CONF_SENSOR_ENTITY)

    @property
    def heater_entity(self) -> str | None:
        """Konfigurierter Heizungs-Aktor."""
        return self._options.get(CONF_HEATER_ENTITY)

    @property
    def hysteresis(self) -> float:
        """Hystereseband."""
        return float(self._options.get(CONF_HYSTERESIS, DEFAULT_HYSTERESIS))

    @property
    def safety_offset(self) -> float:
        """Sicherheits-Offset."""
        return float(self._options.get(CONF_SAFETY_OFFSET, DEFAULT_SAFETY_OFFSET))

    @property
    def language(self) -> str:
        """Sprache der Integration."""
        return self._options.get(CONF_LANGUAGE) or DEFAULT_LANGUAGE

    @property
    def unit(self) -> str:
        """Anzeigeeinheit."""
        return self._options.get(CONF_UNIT) or DEFAULT_UNIT

    @property
    def safety_threshold(self) -> float | None:
        """Übertemperaturschwelle (ungerundet) oder ``None`` ohne Sollwert."""
        if self._setpoint is None:
            return None
        return logic.safety_threshold(self._setpoint, self.safety_offset)

    @property
    def initial_off_pending(self) -> bool:
        """Steht der Startup-OFF noch aus?"""
        return self._initial_off_pending

    @property
    def pending_actuator_tasks(self) -> set[asyncio.Task[Any]]:
        """Laufende Aktor-Tasks (Kopie)."""
        return set(self._pending_actuator_tasks)

    @property
    def loaded(self) -> bool:
        """``False`` nach ``async_shutdown``."""
        return not self._shut_down

    # ------------------------------------------------------------------
    # Lebenszyklus (§7.1)
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Abos, Timer und Stop-Hooks setzen; danach genau ein Sofort-Versuch des Initial-OFF."""
        # Review R4-2: Solange async_start läuft, löst der (bei laufendem HA
        # sofort ausgeführte) async_at_started-Callback keinen Initial-OFF aus.
        self._starting = True
        try:
            hass = self.hass
            self._subscribe_sensor()
            self._subscribe_heater()
            self._tick_unsub = async_track_time_interval(hass, self._on_tick, CONTROL_INTERVAL)
            # Stage 1 (primär): läuft, bevor Integrationen stoppen.
            self._shutdown_job_unsub = hass.async_add_shutdown_job(
                HassJob(self._async_shutdown_job, "brausteuerung_shutdown")
            )
            # Stage 2 (Absicherung): EVENT_HOMEASSISTANT_STOP.
            self._stop_unsub = hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._on_ha_stop
            )
            self._entry.async_on_unload(self._async_remove_stop_hooks)
            self._at_started_unsub = async_at_started(hass, self._on_started)
            # Einziger Sofort-Versuch in async_start (§7.1 Schritt 3).
            self._maybe_initial_off(immediate=True)
        finally:
            self._starting = False

    @callback
    def _async_cancel(self, attr: str) -> None:
        """Meldet ein Abo/Timer-Handle genau einmal ab; Fehler werden nur geloggt."""
        if (unsub := getattr(self, attr)) is None:
            return
        setattr(self, attr, None)
        try:
            unsub()
        except Exception:  # noqa: BLE001 - Abmelden darf das OFF nie verhindern
            _LOGGER.exception("Abmelden von %s fehlgeschlagen", attr)

    @callback
    def _async_remove_stop_hooks(self) -> None:
        """Meldet beide HA-Stop-Pfade ab; mehrfacher Aufruf ist harmlos."""
        if (unsub := self._stop_unsub) is not None:
            self._stop_unsub = None
            unsub()
        if (unsub := self._shutdown_job_unsub) is not None:
            self._shutdown_job_unsub = None
            with contextlib.suppress(ValueError):
                unsub()

    async def _async_shutdown_job(self) -> None:
        """Stage 1: Shutdown-Job von HA."""
        await self.async_shutdown("ha_stop")

    async def _on_ha_stop(self, _event: Event) -> None:
        """Stage 2: EVENT_HOMEASSISTANT_STOP (One-Time-Listener)."""
        # Der One-Time-Listener hat sich beim Feuern schon selbst entfernt.
        self._stop_unsub = None
        await self.async_shutdown("ha_stop")

    async def async_shutdown(self, reason: str) -> None:
        """Abschalten bei Unload/Reload/Stop; OFF wird immer gesendet (§7.1)."""
        if self._shut_down:
            # Idempotent: nur ein weiteres OFF (falls das erste nicht ankam).
            await self._async_turn_off(reason, timeout=SHUTDOWN_OFF_TIMEOUT)
            return
        self._desired_heater_on = False
        self._shut_down = True
        try:
            for attr in _CANCEL_ON_SHUTDOWN:
                self._async_cancel(attr)
        finally:
            await self._async_turn_off(reason, timeout=SHUTDOWN_OFF_TIMEOUT)
        tasks = {task for task in self._pending_actuator_tasks if not task.done()}
        if tasks:
            await asyncio.wait(tasks, timeout=SHUTDOWN_TASK_TIMEOUT)
        self._reset_run_state()
        self._status = STATUS_IDLE
        self._notify_listeners()

    @callback
    def _subscribe_sensor(self) -> None:
        if (sensor := self.sensor_entity) is not None:
            self._sensor_unsub = async_track_state_change_event(
                self.hass, [sensor], self._on_sensor_change
            )

    @callback
    def _subscribe_heater(self) -> None:
        if (heater := self.heater_entity) is not None:
            self._heater_unsub = async_track_state_change_event(
                self.hass, [heater], self._on_heater_change
            )

    # ------------------------------------------------------------------
    # Initial-OFF (§7.1 Schritt 3, Review R4-2/R4-3/R4-4)
    # ------------------------------------------------------------------

    @callback
    def _maybe_initial_off(self, *, immediate: bool) -> None:
        """Löst den Startup-OFF aus, wenn er aussteht und gesendet werden darf."""
        # Auch ein laufender anderer OFF an die aktuelle Heizung (z. B. Watchdog-
        # Retry) zählt: Sein Erfolg beendet pending (R4-4), ein zweiter wäre doppelt.
        if (
            not self._initial_off_pending
            or self._initial_off_inflight
            or self._off_inflight
            or self._shut_down
        ):
            return
        if self.heater_entity is None:
            # Ohne Heizung gibt es nichts auszuschalten.
            self._initial_off_pending = False
            return
        if not self._heater_available():
            return
        if (
            not immediate
            and self._initial_off_failed_at is not None
            and dt_util.utcnow() - self._initial_off_failed_at < INITIAL_OFF_RETRY_INTERVAL
        ):
            return
        self._request_heater(False, "startup", force=True)

    # ------------------------------------------------------------------
    # Ereignisse (§7.2)
    # ------------------------------------------------------------------

    @callback
    @_guard
    def _on_started(self, _hass: HomeAssistant) -> None:
        """async_at_started: Altlasten + Lovelace prüfen, ggf. Initial-OFF."""
        self._entry.async_create_task(
            self.hass,
            self._async_check_legacy_safe(check_resource=True),
            "brausteuerung_legacy_check",
        )
        if not self._starting:
            self._maybe_initial_off(immediate=True)

    async def _async_check_legacy_safe(self, *, check_resource: bool) -> None:
        """Altlasten-Prüfung ohne Auswirkung auf die Regelung."""
        try:
            await migration.async_check_legacy(self.hass, check_resource=check_resource)
        except Exception:  # noqa: BLE001 - Hinweisfunktion, nie fatal
            _LOGGER.exception("Altlasten-Prüfung fehlgeschlagen")

    @callback
    @_guard
    def _on_sensor_change(self, _event: Event[EventStateChangedData]) -> None:
        self._evaluate("sensor")

    @callback
    @_guard
    def _on_heater_change(self, event: Event[EventStateChangedData]) -> None:
        """Heizungs-State: nur Watchdogs und Initial-OFF, keine Regelung."""
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]
        if new_state is not None and new_state.state == STATE_OFF:
            self._off_retry_count = 0
            self._retry_exhausted_logged = False
        self._check_actuator_available()
        if new_state is None or new_state.state in _UNAVAILABLE_STATES:
            return
        # Review R4-3: Sofort nur bei echtem Zustandswechsel oder Wiederkehr;
        # reine Attributänderungen zählen wie ein Tick.
        immediate = (
            old_state is None
            or old_state.state in _UNAVAILABLE_STATES
            or old_state.state != new_state.state
        )
        self._maybe_initial_off(immediate=immediate)

    @callback
    @_guard
    def _on_tick(self, _now: datetime) -> None:
        """5-s-Takt: Initial-OFF-Retry, Watchdogs, Regelung."""
        self._maybe_initial_off(immediate=False)
        self._check_actuator_available()
        if self._status == STATUS_RUNNING and self._check_stale():
            return
        self._evaluate("tick")

    @callback
    def _evaluate(self, trigger: str) -> None:
        """Auswertung mit eigenem Fail-safe (§7.2, Schicht 1)."""
        try:
            self._evaluate_inner(trigger)
        except Exception:
            _LOGGER.exception("Interner Fehler in der Brausteuerung (%s)", trigger)
            self._fail_safe("internal_error")

    @callback
    def _evaluate_inner(self, trigger: str) -> None:
        if self._status not in (STATUS_RUNNING, STATUS_PAUSED):
            return
        before = self._state_signature()
        sensor_state = self._state_of(self.sensor_entity)
        value = logic.parse_sensor_value(sensor_state.state if sensor_state else None)
        heater_state = self._state_of(self.heater_entity)
        heater_on = heater_state is not None and heater_state.state == STATE_ON
        decision = logic.evaluate(
            logic.EvalInput(
                status=self._status,
                phase=self._phase,
                setpoint=self._setpoint,
                sensor_value=value,
                heater_on=heater_on,
                hysteresis=self.hysteresis,
                offset=self.safety_offset,
            )
        )
        safety = decision.overtemp or decision.sensor_invalid or decision.pause

        # Übertemperatur-Episode: genau eine Benachrichtigung pro Episode.
        if decision.overtemp:
            if not self._overtemp_active:
                self._overtemp_active = True
                threshold = self.safety_threshold
                _LOGGER.warning("Übertemperatur erkannt: Heizung aus")
                self._notify(
                    "overtemp",
                    value=f"{value:.1f}",
                    threshold=f"{threshold:.1f}" if threshold is not None else "-",
                    unit=self.unit,
                )
        elif value is not None:
            self._overtemp_active = False

        # (a) Heizungsbefehl nach der Senderegel
        if decision.heater == logic.HEATER_OFF:
            self._request_heater(False, trigger, safety=safety)
        elif decision.heater == logic.HEATER_ON:
            self._request_heater(True, trigger)
        if not safety:
            self._safety_state_active = False

        # (b) Pause (OFF ist oben schon angefordert)
        if decision.pause and self._status == STATUS_RUNNING:
            self._enter_pause(trigger, send_off=decision.heater != logic.HEATER_OFF)

        # (c) Haltephase starten
        if decision.start_hold and self._status == STATUS_RUNNING and self._phase == PHASE_HEATING:
            self._start_hold()

        # (d) Watchdog-Buchführung Kommunikationsverlust
        if decision.sensor_invalid and self._status == STATUS_RUNNING:
            if self._comm_loss_unsub is None:
                _LOGGER.warning("Sensorwert ungültig: Heizung aus, Watchdog läuft")
                self._sensor_invalid_since = dt_util.utcnow()
                self._comm_loss_unsub = async_call_later(
                    self.hass, COMM_LOSS_TIMEOUT, self._on_comm_loss
                )
        elif not decision.sensor_invalid:
            self._sensor_invalid_since = None
            self._async_cancel("_comm_loss_unsub")

        # (e) Listener nur bei Zustandsänderung
        if self._state_signature() != before:
            self._notify_listeners()

    # ------------------------------------------------------------------
    # Ablaufsteuerung (§7.3)
    # ------------------------------------------------------------------

    @_public_op
    async def start(self) -> None:
        """Startet den Brauvorgang bei Rast 1."""
        async with self._data_lock:
            self._ensure_loaded()
            if self._status == STATUS_RUNNING:
                raise BrauError("already_running")
            if await migration.async_check_legacy(self.hass):
                raise BrauError("legacy_automations_active")
            self._ensure_loaded()
            recipe = self._data.recipe
            if not recipe:
                raise BrauError("recipe_empty")
            if self.sensor_entity is None or self.heater_entity is None:
                raise BrauError("not_configured")
            if not self._heater_available():
                raise BrauError("heater_unavailable")
            sensor_state = self._state_of(self.sensor_entity)
            if logic.parse_sensor_value(sensor_state.state if sensor_state else None) is None:
                raise BrauError("sensor_invalid")
            self._reset_run_state()
            self._run_recipe = recipe
            self._step_index = 0
            self._phase = PHASE_HEATING
            self._setpoint = recipe[0].temperature
            self._started_at = dt_util.utcnow()
            # Ab jetzt führt die Regelung die Heizung (kein Startup-OFF mehr).
            self._initial_off_pending = False
            self._status = STATUS_RUNNING
            self._notify_listeners()
            self._evaluate("start")

    @_public_op
    async def stop(self) -> None:
        """Stoppt in jedem Status; nimmt nie den Daten-Lock."""
        self._ensure_loaded()
        self._reset_run_state()
        self._status = STATUS_IDLE
        # Erzwungen: auch bei gemeldetem State off.
        self._request_heater(False, "stop", force=True)
        self._notify_listeners()

    @_public_op
    async def next_rest(self) -> None:
        """Springt genau eine Rast weiter (nur bei running)."""
        self._ensure_loaded()
        if self._status != STATUS_RUNNING:
            raise BrauError("not_running")
        if self._phase == PHASE_HOLDING:
            self._async_cancel("_hold_unsub")
        self._advance(manual=True)

    @callback
    @_guard
    def _on_hold_end(self, _now: datetime) -> None:
        self._hold_unsub = None
        if self._status == STATUS_RUNNING and self._phase == PHASE_HOLDING:
            self._advance(manual=False)

    @callback
    def _advance(self, *, manual: bool) -> None:
        index, done = logic.next_step(self._step_index, len(self._run_recipe))
        self._hold_ends_at = None
        if done:
            self._async_cancel("_hold_unsub")
            self._async_cancel("_comm_loss_unsub")
            self._async_cancel("_actuator_unavailable_unsub")
            self._status = STATUS_DONE
            self._phase = None
            self._setpoint = None
            self._request_heater(False, "done")
            self._notify("brew_complete_manual" if manual else "brew_complete")
            self._notify_listeners()
            return
        self._step_index = index
        self._phase = PHASE_HEATING
        self._setpoint = self._run_recipe[index].temperature
        self._notify_listeners()
        self._evaluate("advance")

    @callback
    def _start_hold(self) -> None:
        rest = self._run_recipe[self._step_index]
        self._phase = PHASE_HOLDING
        self._hold_ends_at = dt_util.utcnow() + timedelta(minutes=rest.duration)
        self._async_cancel("_hold_unsub")
        self._hold_unsub = async_track_point_in_utc_time(
            self.hass, self._on_hold_end, self._hold_ends_at
        )

    @callback
    def _enter_pause(self, reason: str, *, send_off: bool = True) -> None:
        """Pause: OFF, Hold abbrechen, Phase None, Sollwert bleibt (A1)."""
        self._status = STATUS_PAUSED
        self._phase = None
        self._hold_ends_at = None
        self._sensor_invalid_since = None
        self._async_cancel("_hold_unsub")
        self._async_cancel("_comm_loss_unsub")
        self._async_cancel("_actuator_unavailable_unsub")
        if send_off:
            self._request_heater(False, reason, safety=True)
        else:
            self._desired_heater_on = False
            self._safety_state_active = True
        _LOGGER.warning("Brauvorgang pausiert (%s)", reason)
        self._notify_listeners()

    @callback
    def _reset_run_state(self) -> None:
        """Setzt den Laufzeitzustand und alle Ablauf-Timer zurück."""
        self._async_cancel("_hold_unsub")
        self._async_cancel("_comm_loss_unsub")
        self._async_cancel("_actuator_unavailable_unsub")
        self._phase = None
        self._step_index = 0
        self._run_recipe = ()
        self._setpoint = None
        self._hold_ends_at = None
        self._started_at = None
        self._sensor_invalid_since = None
        self._overtemp_active = False
        self._safety_state_active = False
        self._off_retry_count = 0
        self._retry_exhausted_logged = False

    @callback
    def _fail_safe(self, reason: str) -> None:
        """Heizung aus, bei running → Pause, Benachrichtigung (§7.2)."""
        try:
            if self._status == STATUS_RUNNING:
                self._enter_pause(reason)
            else:
                self._request_heater(False, reason, safety=True)
            self._notify("internal_error")
            self._notify_listeners()
        except Exception:  # noqa: BLE001 - keine Rekursion im Fail-safe
            _LOGGER.exception("Fail-safe der Brausteuerung fehlgeschlagen")

    # ------------------------------------------------------------------
    # Sicherheitsfunktionen (§7.4)
    # ------------------------------------------------------------------

    @callback
    @_guard
    def _on_comm_loss(self, _now: datetime) -> None:
        self._comm_loss_unsub = None
        if self._status != STATUS_RUNNING:
            return
        sensor_state = self._state_of(self.sensor_entity)
        if logic.parse_sensor_value(sensor_state.state if sensor_state else None) is not None:
            return
        self._notify("comm_loss", sensor=self.sensor_entity or "-")
        self._enter_pause("comm_loss")

    @callback
    def _check_stale(self) -> bool:
        """S5: Heizung ≥ 10 min an und Sensor ≥ 10 min ohne Bericht → Pause."""
        heater_state = self._state_of(self.heater_entity)
        sensor_state = self._state_of(self.sensor_entity)
        if heater_state is None or sensor_state is None or heater_state.state != STATE_ON:
            return False
        now = dt_util.utcnow()
        if now - heater_state.last_changed < STALE_SENSOR_TIMEOUT:
            return False
        if now - sensor_state.last_reported < STALE_SENSOR_TIMEOUT:
            return False
        _LOGGER.warning("Sensorwert veraltet: Heizung aus, Pause")
        self._notify("stale_sensor", sensor=self.sensor_entity or "-")
        self._enter_pause("stale_sensor")
        return True

    @callback
    def _check_actuator_available(self) -> None:
        """S6 b: Heizung während running ≥ 30 s nicht verfügbar → Pause."""
        if self._status != STATUS_RUNNING or self._heater_available():
            self._async_cancel("_actuator_unavailable_unsub")
            return
        if self._actuator_unavailable_unsub is None:
            self._actuator_unavailable_unsub = async_call_later(
                self.hass, ACTUATOR_UNAVAILABLE_TIMEOUT, self._on_actuator_unavailable
            )

    @callback
    @_guard
    def _on_actuator_unavailable(self, _now: datetime) -> None:
        self._actuator_unavailable_unsub = None
        if self._status != STATUS_RUNNING or self._heater_available():
            return
        self._notify("actuator_unavailable", heater=self.heater_entity or "-")
        self._enter_pause("actuator_unavailable")

    @callback
    def _schedule_verify(self) -> None:
        """Höchstens ein Verify-Timer (§7.4 a)."""
        self._async_cancel("_verify_off_unsub")
        self._verify_off_unsub = async_call_later(
            self.hass, ACTUATOR_VERIFY_DELAY, self._on_verify_off
        )

    @callback
    @_guard
    def _on_verify_off(self, _now: datetime) -> None:
        """Aktor-Watchdog: Heizung nach OFF weiter an → Retry (§7.4 a, R4-4)."""
        self._verify_off_unsub = None
        if self._shut_down or self._desired_heater_on:
            return
        state = self._state_of(self.heater_entity)
        if state is None or state.state in _UNAVAILABLE_STATES:
            _LOGGER.warning("Heizung %s nach OFF nicht verfügbar", self.heater_entity)
            return
        if state.state == STATE_OFF:
            self._off_retry_count = 0
            self._retry_exhausted_logged = False
            return
        if state.state != STATE_ON:
            return
        # Manuell nach dem OFF eingeschaltet → kein Ausfall.
        if self._last_off_sent_at is None or state.last_changed > self._last_off_sent_at:
            return
        self._off_retry_count += 1
        if self._off_retry_count > ACTUATOR_MAX_RETRIES:
            if not self._retry_exhausted_logged:
                self._retry_exhausted_logged = True
                _LOGGER.error(
                    "Heizung %s schaltet nach %d Wiederholungen nicht ab",
                    self.heater_entity,
                    ACTUATOR_MAX_RETRIES,
                )
            return
        if self._off_retry_count == 1:
            self._notify("actuator_failure", heater=self.heater_entity or "-")
        if self._status == STATUS_RUNNING:
            # Das OFF schickt der Retry unten; die Pause selbst sendet nichts.
            self._enter_pause("actuator_failure", send_off=False)
        self._spawn_actuator_task(
            self._async_turn_off("watchdog_retry"), "brausteuerung_watchdog_retry"
        )

    # ------------------------------------------------------------------
    # Aktorpfad (§7.5)
    # ------------------------------------------------------------------

    @callback
    def _spawn_actuator_task(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        """Startet einen Aktorbefehl als Entry-Task (nie Background) und verfolgt ihn."""
        task = self._entry.async_create_task(self.hass, coro, name, eager_start=True)
        self._pending_actuator_tasks.add(task)
        task.add_done_callback(self._pending_actuator_tasks.discard)
        return task

    @callback
    def _request_heater(
        self, on: bool, reason: str, *, force: bool = False, safety: bool = False
    ) -> None:
        """Senderegel mit vier Kategorien (§7.2); setzt immer ``desired_heater_on``."""
        if force and (on or reason not in FORCED_OFF_REASONS):
            raise ValueError(f"unzulässiger erzwungener Befehl: on={on}, reason={reason}")
        self._desired_heater_on = on
        if self.heater_entity is None:
            return
        state = self._state_of(self.heater_entity)
        current = state.state if state is not None else None
        if on:
            if self._shut_down or current == STATE_ON:
                return
            self._spawn_actuator_task(self._async_turn_on(), "brausteuerung_heater_on")
            return
        if force:
            if reason == "startup":
                if not self._heater_available():
                    return
                self._spawn_actuator_task(self._async_initial_off(), "brausteuerung_initial_off")
                return
            if reason == "stop":
                self._spawn_actuator_task(self._async_stop_off(), "brausteuerung_stop_off")
                return
        elif safety:
            transition = not self._safety_state_active
            self._safety_state_active = True
            if transition:
                # Neuer Sicherheitsübergang = neue Watchdog-Episode.
                self._off_retry_count = 0
                self._retry_exhausted_logged = False
            elif current == STATE_OFF:
                return
        elif current == STATE_OFF:
            return
        self._spawn_actuator_task(
            self._async_turn_off(reason), f"brausteuerung_heater_off_{reason}"
        )

    async def _async_initial_off(self) -> None:
        """Erzwungener Startup-OFF; pending beendet ``_async_turn_off`` (R4-4)."""
        self._initial_off_inflight = True
        try:
            await self._async_turn_off("startup")
        finally:
            self._initial_off_inflight = False

    async def _async_stop_off(self) -> None:
        """Erzwungener OFF bei Stop; pending endet über dieselbe Regel (R4-4)."""
        await self._async_turn_off("stop")

    async def _async_turn_off(
        self,
        reason: str,
        entity_id: str | None = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - Shutdown nutzt ein kürzeres Limit
    ) -> bool:
        """Sendet ``switch.turn_off``; wirft nie. ``True`` bei Erfolg (§7.5)."""
        is_current = entity_id is None
        target = self.heater_entity if is_current else entity_id
        if target is None:
            return False
        available_before = is_current and self._heater_available()
        if is_current and not self._shut_down:
            self._async_cancel("_verify_off_unsub")
            self._last_off_sent_at = dt_util.utcnow()
        if is_current:
            self._off_inflight += 1
        sent = True
        try:
            async with asyncio.timeout(SERVICE_CALL_TIMEOUT if timeout is None else timeout):
                await self.hass.services.async_call(
                    "switch",
                    "turn_off",
                    {"entity_id": target},
                    blocking=True,
                    context=Context(),
                )
        except ServiceNotFound as err:
            sent = False
            self._on_turn_off_failed(reason, target, err, service_missing=True)
            return False
        except Exception as err:  # noqa: BLE001 - OFF darf nie werfen
            self._on_turn_off_failed(reason, target, err, service_missing=False)
            return False
        else:
            # Review R4-4: Jeder erfolgreich zugestellte OFF an die aktuelle
            # Heizung bei verfügbarem State beendet den ausstehenden Startup-OFF.
            if is_current and available_before and target == self.heater_entity:
                self._initial_off_pending = False
                self._initial_off_failed_at = None
            return True
        finally:
            if is_current:
                self._off_inflight -= 1
            if sent and is_current and not self._shut_down and target == self.heater_entity:
                self._schedule_verify()

    @callback
    def _on_turn_off_failed(
        self, reason: str, target: str, err: Exception, *, service_missing: bool
    ) -> None:
        """Fehlerauswertung eines OFF-Aufrufs."""
        if reason == "startup":
            if service_missing:
                # switch ist beim HA-Start oft noch nicht registriert; nichts gesendet.
                _LOGGER.debug("Initial-OFF: switch.turn_off noch nicht verfügbar")
                self._initial_off_failed_at = None
            else:
                self._initial_off_failed_at = dt_util.utcnow()
                if self._initial_off_error_logged:
                    _LOGGER.debug(
                        "Initial-OFF an %s erneut fehlgeschlagen (%s)",
                        target,
                        type(err).__name__,
                    )
                else:
                    self._initial_off_error_logged = True
                    _LOGGER.error(
                        "Initial-OFF an %s fehlgeschlagen (%s)", target, type(err).__name__
                    )
        else:
            _LOGGER.error(
                "Ausschalten von %s fehlgeschlagen (%s, %s)",
                target,
                reason,
                type(err).__name__,
            )
        if not self._shut_down and self._status == STATUS_RUNNING:
            self._enter_pause("actuator_error", send_off=False)

    async def _async_turn_on(self) -> None:
        """Sendet ``switch.turn_on``; OFF gewinnt bei Überlappung und Shutdown."""
        target = self.heater_entity
        if not self._desired_heater_on or self._shut_down or target is None:
            return
        try:
            async with asyncio.timeout(SERVICE_CALL_TIMEOUT):
                await self.hass.services.async_call(
                    "switch",
                    "turn_on",
                    {"entity_id": target},
                    blocking=True,
                    context=Context(),
                )
        except Exception as err:  # noqa: BLE001 - Fehler → fail-safe OFF
            _LOGGER.warning("Einschalten von %s fehlgeschlagen (%s)", target, type(err).__name__)
            if self._shut_down:
                return
            if self._status == STATUS_RUNNING:
                self._enter_pause("turn_on_failed")
            else:
                self._desired_heater_on = False
                self._spawn_actuator_task(
                    self._async_turn_off("turn_on_failed"), "brausteuerung_heater_off_failsafe"
                )
            return
        if not self._desired_heater_on or self._shut_down:
            await self._async_turn_off("superseded")

    # ------------------------------------------------------------------
    # Rezept-, Bibliotheks- und Einstellungsoperationen (§7.6)
    # ------------------------------------------------------------------

    @callback
    def _ensure_loaded(self) -> None:
        if self._shut_down:
            raise BrauError("not_loaded")

    @callback
    def _ensure_writable(self) -> None:
        if self._store.read_only:
            raise BrauError("storage_error")

    @callback
    def _ensure_not_running(self) -> None:
        if self._status == STATUS_RUNNING:
            raise BrauError("recipe_locked")

    async def _async_commit(self, new: StoredData) -> None:
        """Speichern, dann übernehmen; bei Fehler bleibt der alte Stand."""
        await self._store.async_save(new)
        self._data = new
        self._notify_listeners()

    async def _apply_recipe_change(self, new_steps: tuple[Rest, ...]) -> None:
        """Rezeptänderung mit Namenslogik (§7.6); Aufrufer hält den Lock."""
        data = self._data
        if new_steps == data.recipe:
            return
        name = data.recipe_name
        if name and logic.is_recipe_unchanged(data.library, name, data.recipe):
            name = logic.next_modified_name(
                data.library, name, MODIFIED_SUFFIX.get(self.language, MODIFIED_SUFFIX["en"])
            )
        await self._async_commit(replace(data, recipe=new_steps, recipe_name=name))

    @callback
    def _check_recipe_op(self) -> None:
        self._ensure_loaded()
        self._ensure_writable()
        self._ensure_not_running()

    @_public_op
    async def add_rest(self, temperature: Any, duration: Any, name: Any = None) -> None:
        """Hängt eine Rast an (leerer Name → „Rast N“)."""
        async with self._data_lock:
            self._check_recipe_op()
            recipe = self._data.recipe
            with _logic_errors():
                rest = Rest(
                    name=logic.validate_rest_name(name, len(recipe) + 1),
                    temperature=logic.validate_temperature(temperature),
                    duration=logic.validate_duration(duration),
                )
                new_steps = logic.add_rest(recipe, rest)
            await self._apply_recipe_change(new_steps)

    @_public_op
    async def update_rest(
        self, index: Any, temperature: Any, duration: Any, name: Any = None
    ) -> None:
        """Ersetzt die Rast an ``index``."""
        async with self._data_lock:
            self._check_recipe_op()
            recipe = self._data.recipe
            with _logic_errors():
                position = index + 1 if isinstance(index, int) else 1
                rest = Rest(
                    name=logic.validate_rest_name(name, position),
                    temperature=logic.validate_temperature(temperature),
                    duration=logic.validate_duration(duration),
                )
                new_steps = logic.update_rest(recipe, index, rest)
            await self._apply_recipe_change(new_steps)

    @_public_op
    async def delete_rest(self, index: Any) -> None:
        """Entfernt die Rast an ``index``."""
        async with self._data_lock:
            self._check_recipe_op()
            with _logic_errors():
                new_steps = logic.delete_rest(self._data.recipe, index)
            await self._apply_recipe_change(new_steps)

    @_public_op
    async def move_rest(self, index: Any, direction: Any) -> None:
        """Verschiebt die Rast um eine Position (−1 hoch, +1 runter)."""
        async with self._data_lock:
            self._check_recipe_op()
            with _logic_errors():
                new_steps = logic.move_rest(self._data.recipe, index, direction)
            await self._apply_recipe_change(new_steps)

    @_public_op
    async def clear_recipe(self) -> None:
        """Leert das aktive Rezept."""
        async with self._data_lock:
            self._check_recipe_op()
            await self._apply_recipe_change(())

    @_public_op
    async def load_recipe(self, name: Any) -> None:
        """Lädt ein Bibliotheksrezept als aktives Rezept (ohne Suffix)."""
        async with self._data_lock:
            self._check_recipe_op()
            entry = logic.find_recipe(self._data.library, name)
            if entry is None:
                raise BrauError("recipe_not_found")
            await self._async_commit(
                replace(self._data, recipe=entry.steps, recipe_name=entry.name)
            )

    @_public_op
    async def save_recipe(self, name: Any, overwrite: bool = False) -> None:
        """Speichert das aktive Rezept in der Bibliothek (auch bei running)."""
        async with self._data_lock:
            self._ensure_loaded()
            self._ensure_writable()
            with _logic_errors():
                clean = logic.validate_recipe_name(name, allow_empty=False)
                if logic.find_recipe(self._data.library, clean) is not None and not overwrite:
                    raise BrauError("name_exists")
                library = logic.upsert_recipe(
                    self._data.library, LibraryRecipe(clean, self._data.recipe)
                )
            await self._async_commit(replace(self._data, library=library, recipe_name=clean))

    @_public_op
    async def delete_recipe(self, name: Any) -> None:
        """Entfernt einen Bibliothekseintrag."""
        async with self._data_lock:
            self._ensure_loaded()
            self._ensure_writable()
            with _logic_errors():
                library = logic.remove_recipe(self._data.library, name)
            await self._async_commit(replace(self._data, library=library))

    @_public_op
    async def import_library(self, recipes: Any, active_recipe_name: Any = None) -> dict[str, int]:
        """Übernimmt Bibliothekseinträge ohne Überschreiben (§10.2)."""
        async with self._data_lock:
            self._ensure_loaded()
            self._ensure_writable()
            valid, skipped = logic.validate_import(recipes)
            library = list(self._data.library)
            imported = 0
            for recipe in valid:
                if (
                    logic.find_recipe(library, recipe.name) is not None
                    or len(library) >= MAX_LIBRARY_RECIPES
                ):
                    skipped += 1
                    continue
                library.append(recipe)
                imported += 1
            name = self._data.recipe_name
            if not name and active_recipe_name is not None:
                try:
                    name = logic.validate_recipe_name(active_recipe_name)
                except LogicError:
                    name = ""
            if imported or name != self._data.recipe_name:
                await self._async_commit(
                    replace(self._data, library=tuple(library), recipe_name=name)
                )
            return {"imported": imported, "skipped": skipped}

    async def async_import_legacy(self, raw: Any) -> None:
        """Einmalige Übernahme aus ``entry.data["legacy_import"]`` (§10.1)."""
        async with self._data_lock:
            if self._data.legacy_migrated or self._store.read_only:
                return
            recipe, name = migration.parse_legacy_import(raw)
            new = replace(self._data, legacy_migrated=True)
            if recipe is not None:
                new = replace(new, recipe=recipe)
            if name is not None:
                new = replace(new, recipe_name=name)
            await self._async_commit(new)

    @_public_op
    async def async_update_settings(self, **changes: Any) -> None:
        """Validiert und übernimmt Einstellungen (Options + Live-Übernahme)."""
        self._ensure_loaded()
        validators: dict[str, Callable[[Any], Any]] = {
            CONF_SENSOR_ENTITY: lambda v: logic.validate_entity_id(v, "sensor"),
            CONF_HEATER_ENTITY: lambda v: logic.validate_entity_id(v, "switch"),
            CONF_HYSTERESIS: logic.validate_hysteresis,
            CONF_SAFETY_OFFSET: logic.validate_offset,
            CONF_LANGUAGE: logic.validate_language,
            CONF_UNIT: logic.validate_unit,
        }
        clean: dict[str, Any] = {}
        with _logic_errors():
            for key, value in changes.items():
                if key not in validators:
                    raise BrauError("internal_error")
                clean[key] = validators[key](value)
        if self._status == STATUS_RUNNING and any(
            key in clean and clean[key] != self._options.get(key)
            for key in (CONF_SENSOR_ENTITY, CONF_HEATER_ENTITY)
        ):
            raise BrauError("settings_locked")
        new = {**self._entry.options, **clean}
        if new != dict(self._entry.options):
            self.hass.config_entries.async_update_entry(self._entry, options=new)
        self.apply_options(new)

    @callback
    @_guard
    def apply_options(self, options: dict[str, Any]) -> None:
        """Übernimmt Einstellungen live, idempotent per Diff (§7.6)."""
        if self._shut_down:
            return
        new = {**_OPTION_DEFAULTS, **options}
        old = self._options
        if new == old:
            return
        self._options = new
        if new[CONF_SENSOR_ENTITY] != old[CONF_SENSOR_ENTITY]:
            self._async_cancel("_sensor_unsub")
            self._subscribe_sensor()
        old_heater = old[CONF_HEATER_ENTITY]
        if new[CONF_HEATER_ENTITY] != old_heater:
            # (1) Watchdogs der alten Heizung beenden
            self._async_cancel("_verify_off_unsub")
            self._async_cancel("_actuator_unavailable_unsub")
            self._off_retry_count = 0
            self._retry_exhausted_logged = False
            # (2) erzwungener OFF an die alte Heizung (ohne Watchdog)
            if old_heater is not None:
                self._spawn_actuator_task(
                    self._async_turn_off("settings_old_heater", entity_id=old_heater),
                    "brausteuerung_heater_off_old",
                )
            # (3) Abo auf die neue Heizung
            self._async_cancel("_heater_unsub")
            self._subscribe_heater()
            # (4) Initial-OFF-Regeln für die neue Heizung
            self._initial_off_pending = True
            self._initial_off_failed_at = None
            self._initial_off_error_logged = False
            self._maybe_initial_off(immediate=True)
        self._evaluate("settings")
        self._notify_listeners()

    # ------------------------------------------------------------------
    # Snapshot und Listener (§7.7)
    # ------------------------------------------------------------------

    @callback
    def snapshot(self) -> dict[str, Any]:
        """Zustand für Karte und Entitäten."""
        if self._shut_down:
            return {"loaded": False}
        threshold = self.safety_threshold
        active = self._run_recipe if self._status in (STATUS_RUNNING, STATUS_PAUSED) else None
        registry = er.async_get(self.hass)
        return {
            "version": self._version,
            "loaded": True,
            "status": self._status,
            "phase": self._phase,
            "step_index": self._step_index,
            "rest_count": len(active if active is not None else self._data.recipe),
            "hold_ends_at": self._hold_ends_at.isoformat() if self._hold_ends_at else None,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "setpoint": self._setpoint,
            "safety_threshold": round(threshold, 2) if threshold is not None else None,
            "recipe": logic.recipe_to_list(self._data.recipe),
            "recipe_name": self._data.recipe_name,
            "library": [entry.to_dict() for entry in self._data.library],
            "settings": {key: self._options.get(key) for key in _OPTION_DEFAULTS},
            "entities": {
                "setpoint": registry.async_get_entity_id(
                    Platform.SENSOR, DOMAIN, f"{self._entry.entry_id}_target_temperature"
                )
            },
        }

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Registriert einen Listener; Rückgabe meldet ihn ab."""
        self._listeners.append(update_callback)

        @callback
        def remove_listener() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(update_callback)

        return remove_listener

    @callback
    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners):
            try:
                update_callback()
            except Exception:  # noqa: BLE001 - ein Listener darf die Regelung nie stören
                _LOGGER.exception("Listener der Brausteuerung fehlgeschlagen")

    # ------------------------------------------------------------------
    # Hilfen
    # ------------------------------------------------------------------

    @callback
    def _state_signature(self) -> tuple[Any, ...]:
        return (
            self._status,
            self._phase,
            self._step_index,
            self._setpoint,
            self._hold_ends_at,
            self._started_at,
        )

    @callback
    def _state_of(self, entity_id: str | None) -> State | None:
        return self.hass.states.get(entity_id) if entity_id else None

    @callback
    def _heater_available(self) -> bool:
        state = self._state_of(self.heater_entity)
        return state is not None and state.state not in _UNAVAILABLE_STATES

    @callback
    def _notify(self, kind: str, **placeholders: str) -> None:
        """Persistente Benachrichtigung mit fester ID je Art."""
        title, message = messages.render(kind, self.language, **placeholders)
        persistent_notification.async_create(
            self.hass, message, title, notification_id=f"{NOTIFICATION_ID_PREFIX}{kind}"
        )
