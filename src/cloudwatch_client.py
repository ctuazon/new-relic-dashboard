"""Thin client around CloudWatch Logs for the realms' shared ECS log group."""
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .aws import AwsClient, AwsError

MAX_PAGES = 10  # FilterLogEvents returns up to 10,000 events a page; past this a count is a lower bound

# nginx error-log lines end with host: "www.site.com"; access-log lines carry host="www.site.com".
_HOST_RE = re.compile(r'host(?:: |=)"([^"]+)"')
_LEVEL_RE = re.compile(r"marketplace\.([A-Z]+): ")
# The Monolog message runs up to its context JSON / "[]", the next batched PHP message, or
# nginx's own trailer, whichever comes first.
_MESSAGE_RE = re.compile(r"(.*?)(?: \[\]| \{\"|PHP message:|\" while reading|$)", re.DOTALL)
_LEVEL_RANK = {"DEBUG": 0, "INFO": 1, "NOTICE": 2, "WARNING": 3, "ERROR": 4,
               "CRITICAL": 5, "ALERT": 6, "EMERGENCY": 7}
_REQUEST_RE = re.compile(r'request: "([A-Z]+) (\S+) HTTP/[\d.]+"')


@dataclass
class LogEvent:
    event_id: str
    timestamp: float  # epoch seconds
    host: str
    level: str
    message: str
    method: str
    url: str
    task_id: str
    raw: str


def parse_event(raw_event: dict) -> LogEvent:
    raw = raw_event.get("message") or ""
    host = _HOST_RE.search(raw)
    request = _REQUEST_RE.search(raw)
    # nginx batches several PHP messages into one line (often a NOTICE before the ERROR that
    # matched the filter), so report the most severe one rather than the first.
    levels = list(_LEVEL_RE.finditer(raw))
    if "PHP Fatal" in raw:
        level_name, message_text = "FATAL", raw[raw.index("PHP Fatal"):]
    elif levels:
        worst = max(levels, key=lambda m: _LEVEL_RANK.get(m.group(1), -1))
        level_name, message_text = worst.group(1), raw[worst.end():]
    else:
        level_name, message_text = "UNKNOWN", raw
    message = _MESSAGE_RE.match(message_text)
    return LogEvent(
        event_id=raw_event.get("eventId") or "",
        timestamp=(raw_event.get("timestamp") or 0) / 1000.0,
        host=host.group(1).lower() if host else "",
        level=level_name,
        message=" ".join((message.group(1) if message else message_text).split()),
        method=request.group(1) if request else "",
        url=request.group(2) if request else "",
        # ECS streams are named prefix/container/<task id>.
        task_id=(raw_event.get("logStreamName") or "").rsplit("/", 1)[-1],
        raw=raw,
    )


class CloudWatchClient(AwsClient):
    service = "logs"
    label = "CloudWatch"

    def __init__(self, profile: str, region: str, log_group: str):
        super().__init__(profile, region)
        self.log_group = log_group

    def _client_error_message(self, code, error):
        if code == "ResourceNotFoundException":
            return f"Log group '{self.log_group}' not found in {self.region}."
        if code == "AccessDeniedException":
            return f"Profile '{self.profile}' isn't allowed to read '{self.log_group}'."
        return super()._client_error_message(code, error)

    def _probe(self) -> str:
        resp = self._call(self._api().describe_log_groups, logGroupNamePrefix=self.log_group, limit=5)
        if not any(g.get("logGroupName") == self.log_group for g in resp.get("logGroups", [])):
            raise AwsError(f"log group '{self.log_group}' not found in {self.region}.")
        return f"{self.log_group} reachable."

    def get_events(self, filter_pattern: str, lookback_seconds: int) -> Tuple[List[LogEvent], bool]:
        """Every event matching the pattern over the lookback window, newest first, for all realms
        at once (one paginated call rather than one per realm). The second value is True if the
        page cap cut the result short."""
        logs = self._api()
        kwargs = {
            "logGroupName": self.log_group,
            "startTime": int((time.time() - lookback_seconds) * 1000),
            "filterPattern": filter_pattern,
        }
        events: List[LogEvent] = []
        token: Optional[str] = None
        for _ in range(MAX_PAGES):
            if token:
                kwargs["nextToken"] = token
            resp = self._call(logs.filter_log_events, **kwargs)
            events.extend(parse_event(e) for e in resp.get("events", []))
            token = resp.get("nextToken")
            if not token:
                break
        events.sort(key=lambda e: e.timestamp, reverse=True)
        return events, token is not None
