"""Background polling engine: queries New Relic per location/group and CloudWatch Logs per
realm and flags anomalies, and pulls a CloudTrail feed of ECS deploys/changes (display only)."""
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from .cloudtrail_client import ChangeEvent, CloudTrailClient
from .cloudwatch_client import CloudWatchClient, LogEvent
from .config import AppConfig, Group, Location
from .history import SeriesHistory
from .logger import SessionLogger
from .newrelic_client import NewRelicClient, NewRelicError

MAX_ERROR_RECORDS_PER_LOCATION = 200
MAX_LOG_RECORDS_PER_LOCATION = 200
MAX_AUTOSCALING_EVENTS = 1000  # deploys and other changes are all kept for the lookback window
# CloudTrail usually delivers an event within ~5 minutes but can take up to ~15, so each poll
# re-reads that much overlap and drops what it has already seen.
CLOUDTRAIL_DELIVERY_OVERLAP_SECONDS = 20 * 60


def logs_key(location_name: str) -> str:
    return f"cw:{location_name}"


def logs_name(location_name: str) -> str:
    return f"{location_name} logs"


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
    kind: str  # "location", "group" or "logs"
    value: Optional[float] = None
    threshold: Optional[float] = None
    status: str = "PENDING"  # PENDING, OK, ANOMALY, ERROR
    last_updated: Optional[float] = None
    error: Optional[str] = None
    error_records: List[ErrorRecord] = field(default_factory=list)  # newest first
    log_records: List[LogEvent] = field(default_factory=list)  # "logs" series only, newest first

    @property
    def error_inbox_count(self) -> int:
        return len(self.error_records)

    @property
    def records(self) -> list:
        """Whichever inbox this series has: CloudWatch lines for a logs series, else NR errors."""
        return self.log_records if self.kind == "logs" else self.error_records

    @property
    def newest_record_id(self) -> Optional[str]:
        if self.kind == "logs":
            return self.log_records[0].event_id if self.log_records else None
        return self.error_records[0].guid if self.error_records else None


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
        self._seen_log_ids: Set[str] = set()
        self._last_cw_error: Optional[str] = None
        self._stop = threading.Event()
        self._manual_poll = threading.Event()
        self._manual_poll_cw = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cw_thread: Optional[threading.Thread] = None
        self.cloudwatch: Optional[CloudWatchClient] = None
        self.cloudtrail: Dict[tuple, CloudTrailClient] = {}  # (profile, region) -> client
        self._ct_thread: Optional[threading.Thread] = None
        self._manual_poll_ct = threading.Event()
        # CloudTrail feed state, read by the UI: newest first, plus the last error and check time.
        self.changes: List[ChangeEvent] = []
        self.changes_error: Optional[str] = None
        self.changes_checked_at: Optional[float] = None
        self._seen_change_ids: Set[str] = set()
        self._ct_since: Optional[float] = None
        self._changes_lock = threading.Lock()
        self._generation = 0  # bumped per start() so a CloudWatch thread from a previous run exits

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def make_cloudwatch_client(self) -> Optional[CloudWatchClient]:
        s = self.cfg.settings
        if not s.cloudwatch_enabled:
            return None
        return CloudWatchClient(s.cloudwatch_aws_profile, s.cloudwatch_region, s.cloudwatch_log_group)

    def make_cloudtrail_clients(self) -> Dict[tuple, CloudTrailClient]:
        """One client per (profile, region) among the targets: each is queried once per poll and
        its events matched to every target that uses it."""
        s = self.cfg.settings
        if not s.cloudtrail_enabled:
            return {}
        return {(t.profile, t.region): CloudTrailClient(t.profile, t.region) for t in s.cloudtrail_targets}

    def aws_clients(self) -> list:
        """Every AWS client a fresh start would use, for connection tests and AWS Login."""
        clients = list(self.make_cloudtrail_clients().values())
        cw = self.make_cloudwatch_client()
        return ([cw] if cw else []) + clients

    def _seed_states(self) -> None:
        """Creates every series up front, in display order (each realm's logs row right below
        it), so two polling threads adding states never reshuffle the dashboard."""
        ordered: Dict[str, SeriesState] = {}
        for loc in self.cfg.locations:
            if not loc.enabled:
                continue
            for key, name, kind in ((f"loc:{loc.name}", loc.name, "location"),
                                    (logs_key(loc.name), logs_name(loc.name), "logs")):
                if kind == "logs" and self.cloudwatch is None:
                    continue
                ordered[key] = self.states.get(key) or SeriesState(key=key, name=name, kind=kind)
        for group in self.cfg.groups:
            key = f"grp:{group.name}"
            ordered[key] = self.states.get(key) or SeriesState(key=key, name=group.name, kind="group")
        self.states = ordered

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._manual_poll.clear()
        self._manual_poll_cw.clear()
        self._manual_poll_ct.clear()
        self.cloudtrail = self.make_cloudtrail_clients()
        self.changes, self.changes_error, self.changes_checked_at = [], None, None
        self._seen_change_ids, self._ct_since = set(), None
        self._seen_errors = {}
        self._seen_log_ids = set()
        self._last_cw_error = None
        self.cloudwatch = self.make_cloudwatch_client()
        self._seed_states()
        for state in self.states.values():
            state.error_records = []
            state.log_records = []
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._generation += 1
        if self.cloudwatch is not None:
            self._cw_thread = threading.Thread(target=self._aws_loop, daemon=True, args=(
                self._generation, self.poll_cloudwatch_once, "cloudwatch_poll_interval_seconds",
                self._manual_poll_cw))
            self._cw_thread.start()
        if self.cloudtrail:
            self._ct_thread = threading.Thread(target=self._aws_loop, daemon=True, args=(
                self._generation, self.poll_cloudtrail_once, "cloudtrail_poll_interval_seconds",
                self._manual_poll_ct))
            self._ct_thread.start()
        self.logger.event("INFO", "Monitoring started.")

    def stop(self) -> None:
        self._stop.set()
        self.logger.event("INFO", "Monitoring stopped.")

    def refresh_now(self) -> None:
        """Wakes the polling loops immediately instead of waiting out the current interval."""
        self._manual_poll.set()
        self._manual_poll_cw.set()
        self._manual_poll_ct.set()

    def reset_aws_clients(self) -> None:
        """After AWS Login: drop cached clients so the next poll uses the fresh credentials."""
        for client in [self.cloudwatch, *self.cloudtrail.values()]:
            if client is not None:
                client.reset()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._manual_poll.clear()
            self._interruptible_wait(self.cfg.settings.poll_interval_seconds, self._manual_poll)

    def _aws_loop(self, generation: int, poll: Callable[[], None], interval_setting: str,
                  wake: threading.Event) -> None:
        """CloudWatch and CloudTrail each run on their own thread so AWS latency, throttling or an
        expired SSO login can never hold up the New Relic polling. A thread left over from a
        previous start exits once it sees the generation has moved on."""
        while not self._stop.is_set() and generation == self._generation:
            poll()
            wake.clear()
            self._interruptible_wait(getattr(self.cfg.settings, interval_setting), wake)

    def _interruptible_wait(self, seconds: float, wake: threading.Event) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._stop.wait(0.2):
                return
            if wake.is_set():
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

    def poll_cloudwatch_once(self) -> None:
        """One FilterLogEvents sweep of the shared log group covers every realm: lines are
        split by their nginx host, counted over the lookback window (the alerting value) and
        any not seen before are added to that realm's log inbox. Re-reading the whole window
        each time (rather than only since the last poll) keeps counts right when CloudWatch
        delivers lines late."""
        settings = self.cfg.settings
        locations = [loc for loc in self.cfg.locations if loc.enabled]
        try:
            events, capped = self.cloudwatch.get_events(
                settings.cloudwatch_filter_pattern, settings.cloudwatch_lookback_seconds)
        except Exception as e:
            message = str(e)
            for loc in locations:
                state = self.states.get(logs_key(loc.name))
                if state is not None:
                    state.status, state.error, state.last_updated = "ERROR", message, time.time()
            if message != self._last_cw_error:  # an expired login would otherwise log every minute
                self.logger.event("ERROR", f"CloudWatch: {message}")
            self._last_cw_error = message
            self.on_update()
            return
        if self._last_cw_error:
            self.logger.event("INFO", "CloudWatch: reading logs again.")
            self._last_cw_error = None
        if capped:
            self.logger.event("INFO", "CloudWatch: hit the page limit; log counts this poll are a lower bound.")

        excludes = [p for p in settings.cloudwatch_exclude_patterns if p]
        by_host: Dict[str, List[LogEvent]] = {}
        for event in events:
            if any(p in event.raw for p in excludes):
                continue
            by_host.setdefault(event.host, []).append(event)

        window_ids: Set[str] = set()
        for loc in locations:
            realm_events = by_host.get(loc.cloudwatch_host, [])
            window_ids.update(e.event_id for e in realm_events)
            key = logs_key(loc.name)
            count = float(len(realm_events))
            self._evaluate(
                key=key, name=logs_name(loc.name), kind="logs",
                fetch=lambda n=count: n,
                use_custom=False, custom_value=0.0, record_for_combine=None,
                volume_fetch=lambda n=count: n,
                min_volume=settings.min_log_errors_for_alert, volume_label="error lines",
                fallback_threshold=settings.cloudwatch_fallback_threshold,
            )
            new = [e for e in realm_events if e.event_id not in self._seen_log_ids]
            if new:
                state = self.states[key]
                state.log_records = (new + state.log_records)[:MAX_LOG_RECORDS_PER_LOCATION]
                latest = new[0]
                self.logger.event(
                    "CLOUDWATCH", f"{loc.name}: {len(new)} new error line(s), latest "
                                  f"{latest.level} '{latest.message[:200]}' on {latest.url or '-'}")
        # Lines only ever come back while inside the window, so that's all the seen-set needs.
        self._seen_log_ids = window_ids
        self.on_update()

    def poll_cloudtrail_once(self) -> None:
        """Pulls ECS change events since the last poll (the whole lookback window on the first)
        and keeps the ones on a configured target. Display only: nothing here raises alerts.

        Each AWS account/region is read on its own thread (CloudTrail's rate limit is per account
        and region) and shown as soon as it's done, so prod changes appear within seconds even
        while a busy PR account is still loading its week of history."""
        settings = self.cfg.settings
        now = time.time()
        since = self._ct_since if self._ct_since is not None else now - settings.cloudtrail_lookback_hours * 3600
        errors: List[str] = []
        workers = [threading.Thread(target=self._poll_cloudtrail_account, daemon=True,
                                    args=(client, since - CLOUDTRAIL_DELIVERY_OVERLAP_SECONDS, now, errors))
                   for client in list(self.cloudtrail.values())]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        error = "; ".join(dict.fromkeys(errors)) or None
        if error and error != self.changes_error:
            self.logger.event("ERROR", f"CloudTrail: {error}")
        self.changes_error = error
        if not errors:
            self._ct_since = now  # only move forward once every account was read
        self.changes_checked_at = now
        self.on_update()

    def _poll_cloudtrail_account(self, client: CloudTrailClient, start: float, end: float,
                                 errors: List[str]) -> None:
        settings = self.cfg.settings
        targets = [t for t in settings.cloudtrail_targets if (t.profile, t.region) == (client.profile, client.region)]
        try:
            events = client.get_changes([t.cluster for t in targets], start, end)
        except Exception as e:
            with self._changes_lock:
                errors.append(str(e))
            return
        with self._changes_lock:
            new: List[ChangeEvent] = []
            for event in events:
                if event.event_id in self._seen_change_ids:
                    continue
                target = next((t for t in targets if t.matches(event.cluster, event.service)), None)
                if target is None:
                    continue
                self._seen_change_ids.add(event.event_id)
                event.target = target.label
                event.on_charts = target.show_on_charts
                new.append(event)
            if not new:
                return
            cutoff = end - settings.cloudtrail_lookback_hours * 3600
            merged = sorted(new + self.changes, key=lambda e: e.timestamp, reverse=True)
            # Auto Scaling resizes services hundreds of times a week; cap only those, so they can
            # never push a real deploy out of the feed.
            autoscaling = 0
            kept = []
            for e in merged:
                if e.timestamp < cutoff:
                    continue
                if e.is_autoscaling:
                    autoscaling += 1
                    if autoscaling > MAX_AUTOSCALING_EVENTS:
                        continue
                kept.append(e)
            self.changes = kept  # replaced, never mutated, so the UI can read it without the lock
            if self.changes_checked_at is None:
                self.changes_checked_at = end  # first results are in; the tab stops saying "Loading"
        for event in sorted(new, key=lambda e: e.timestamp):
            if not event.is_autoscaling:
                self.logger.event("CHANGE", f"{event.target} {event.service}: {event.summary} by {event.actor}")
        self.on_update()

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
                  min_volume: int = 0, volume_label: str = "transactions",
                  fallback_threshold: Optional[float] = None) -> None:
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
            settings.flat_fallback_threshold_percent if fallback_threshold is None else fallback_threshold,
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
                        f"— only {volume:.0f} {volume_label} in window (min {min_volume}).",
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
