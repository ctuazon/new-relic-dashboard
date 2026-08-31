"""Background polling engine: queries New Relic per location/group and flags anomalies."""
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from .config import AppConfig, Group, Location
from .history import SeriesHistory
from .logger import SessionLogger
from .newrelic_client import NewRelicClient, NewRelicError

MAX_ERROR_RECORDS_PER_LOCATION = 200


@dataclass
class ErrorRecord:
    guid: str
    timestamp: float  # epoch seconds
    error_class: str
    error_message: str
    endpoint: str
    method: str
    status_code: Optional[int]
    container: str


@dataclass
class SeriesState:
    key: str
    name: str
    kind: str  # "location" or "group"
    value: Optional[float] = None
    threshold: Optional[float] = None
    status: str = "PENDING"  # PENDING, OK, ANOMALY, ERROR
    last_updated: Optional[float] = None
    error: Optional[str] = None
    error_records: List[ErrorRecord] = field(default_factory=list)  # newest first

    @property
    def error_inbox_count(self) -> int:
        return len(self.error_records)


class MonitorEngine:
    def __init__(self, client: NewRelicClient, cfg: AppConfig, logger: SessionLogger,
                 on_update: Callable[[], None],
                 on_anomaly: Callable[[str, str, float, float], None],
                 on_recover: Callable[[str], None]):
        self.client = client
        self.cfg = cfg
        self.logger = logger
        self.on_update = on_update
        self.on_anomaly = on_anomaly
        self.on_recover = on_recover

        self.histories: Dict[str, SeriesHistory] = {}
        self.states: Dict[str, SeriesState] = {}
        self._seen_errors: Dict[str, Set[tuple]] = {}
        self._stop = threading.Event()
        self._manual_poll = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._manual_poll.clear()
        self._seen_errors = {}
        for state in self.states.values():
            state.error_records = []
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.logger.event("INFO", "Monitoring started.")

    def stop(self) -> None:
        self._stop.set()
        self.logger.event("INFO", "Monitoring stopped.")

    def refresh_now(self) -> None:
        """Wakes the polling loop immediately instead of waiting out the current interval."""
        self._manual_poll.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._manual_poll.clear()
            self._interruptible_wait(self.cfg.settings.poll_interval_seconds)

    def _interruptible_wait(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._stop.wait(0.2):
                return
            if self._manual_poll.is_set():
                return

    def poll_once(self) -> None:
        location_values: Dict[str, float] = {}
        for loc in self.cfg.locations:
            if not loc.enabled:
                continue
            self._evaluate(
                key=f"loc:{loc.name}", name=loc.name, kind="location",
                fetch=lambda l=loc: self.client.run_nrql_value(l.nrql),
                use_custom=loc.use_custom_threshold, custom_value=loc.custom_threshold,
                record_for_combine=location_values,
                volume_fetch=lambda l=loc: self.client.get_transaction_count(l.app_name, l.lookback_seconds),
                min_volume=self.cfg.settings.min_transactions_for_alert,
            )
            if self.cfg.settings.error_inbox_enabled:
                self._check_error_inbox(loc)

        for group in self.cfg.groups:
            self._evaluate_group(group, location_values)

        self.on_update()

    def _check_error_inbox(self, loc: Location) -> None:
        """Polls TransactionError (the Errors Inbox source data) for individual error
        occurrences, keeping full per-event detail (endpoint, method, status, container) for
        newly-seen ones. Seen-set and records live in memory for this session only, so
        nothing is written once monitoring stops or the app is closed."""
        app_name = loc.app_name
        settings = self.cfg.settings
        seen = self._seen_errors.setdefault(loc.name, set())
        key = f"loc:{loc.name}"
        state = self.states.setdefault(key, SeriesState(key=key, name=loc.name, kind="location"))
        try:
            rows = self.client.get_error_events(
                app_name, settings.error_inbox_lookback_seconds,
                settings.error_inbox_container_attribute,
            )
        except Exception as e:
            self.logger.event("ERROR", f"{loc.name} [errors inbox]: {e}")
            return

        container_attr = settings.error_inbox_container_attribute
        new_records = []
        for row in rows:
            guid = row.get("guid")
            if not guid or guid in seen:
                continue
            seen.add(guid)
            new_records.append(ErrorRecord(
                guid=guid,
                timestamp=(row.get("timestamp") or 0) / 1000.0,
                error_class=row.get("error.class") or "Unknown",
                error_message=row.get("error.message") or "",
                endpoint=row.get("request.uri") or row.get("transactionName") or "",
                method=row.get("request.method") or "",
                status_code=row.get("http.statusCode"),
                container=row.get(container_attr) or "unknown",
            ))

        if not new_records:
            return
        state.error_records = (new_records + state.error_records)[:MAX_ERROR_RECORDS_PER_LOCATION]
        latest = new_records[0]
        self.logger.event(
            "ERROR_INBOX",
            f"{loc.name}: {len(new_records)} new error(s), latest '{latest.error_class}: "
            f"{latest.error_message}' on {latest.endpoint} ({container_attr}={latest.container})",
        )

    def _evaluate_group(self, group: Group, location_values: Dict[str, float]) -> None:
        key = f"grp:{group.name}"

        def fetch():
            if group.custom_nrql:
                return self.client.run_nrql_value(group.custom_nrql)
            members = group.members or list(location_values.keys())
            vals = [location_values[m] for m in members if m in location_values]
            if not vals:
                raise NewRelicError("No member location values available yet to combine.")
            return sum(vals) / len(vals)

        self._evaluate(
            key=key, name=group.name, kind="group", fetch=fetch,
            use_custom=group.use_custom_threshold, custom_value=group.custom_threshold,
            record_for_combine=None,
        )

    def _evaluate(self, key: str, name: str, kind: str, fetch: Callable[[], float],
                  use_custom: bool, custom_value: float,
                  record_for_combine: Optional[Dict[str, float]],
                  volume_fetch: Optional[Callable[[], float]] = None,
                  min_volume: int = 0) -> None:
        state = self.states.setdefault(key, SeriesState(key=key, name=name, kind=kind))
        history = self.histories.setdefault(key, SeriesHistory())
        settings = self.cfg.settings

        try:
            value = fetch()
        except Exception as e:
            state.status = "ERROR"
            state.error = str(e)
            state.last_updated = time.time()
            self.logger.event("ERROR", f"{name}: {e}")
            return

        if record_for_combine is not None:
            record_for_combine[name] = value

        threshold = custom_value if use_custom else history.auto_threshold(
            settings.threshold_stdev_multiplier, settings.min_samples_for_auto_threshold,
            settings.flat_fallback_threshold_percent,
        )

        history.add(time.time(), value)
        was_anomaly = state.status == "ANOMALY"
        is_anomaly = value > threshold

        if is_anomaly and volume_fetch is not None and min_volume > 0:
            try:
                volume = volume_fetch()
                if volume < min_volume:
                    is_anomaly = False
                    self.logger.event(
                        "INFO",
                        f"{name}: {value:.2f} exceeded threshold {threshold:.2f} but suppressed "
                        f"— only {volume:.0f} transactions in window (min {min_volume}).",
                    )
            except Exception as e:
                self.logger.event("ERROR", f"{name} [volume check]: {e}")

        state.value = value
        state.threshold = threshold
        state.last_updated = time.time()
        state.error = None
        state.status = "ANOMALY" if is_anomaly else "OK"

        self.logger.reading(name, value, threshold, state.status)

        if is_anomaly and not was_anomaly:
            self.logger.event("ALERT", f"Anomaly in {name}: {value:.2f} > threshold {threshold:.2f}")
            self.on_anomaly(key, name, value, threshold)
        elif not is_anomaly and was_anomaly:
            self.logger.event("INFO", f"{name} recovered: {value:.2f} <= threshold {threshold:.2f}")
            self.on_recover(key)
