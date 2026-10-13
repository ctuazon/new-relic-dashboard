"""Container logs for the PR environments in the CloudTrail feed: finds where each container of
an ECS service writes (its task definition's awslogs group and stream prefix) and reads them
back from CloudWatch, like `tail` on the containers without needing a shell in them."""
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .aws import AwsClient, AwsError

MAX_PAGES = 20  # filter_log_events pages per fetch; each is up to 10,000 events / 1 MB
MAX_EVENTS = 5000  # most lines shown at once; the newest are kept


@dataclass
class ContainerLog:
    """Where one container's stdout/stderr lands in CloudWatch."""
    container: str
    log_group: str
    stream_prefix: str  # awslogs streams are named <prefix>/<container>/<task id>


@dataclass
class LogLine:
    timestamp: float  # epoch seconds
    container: str
    message: str


class PrLogsClient(AwsClient):
    service = "logs"
    label = "PR logs"

    def _client_error_message(self, code, error):
        if code == "AccessDeniedException":
            return f"Profile '{self.profile}' isn't allowed to read these logs ({error})."
        if code == "ResourceNotFoundException":
            return "The log group no longer exists (its retention may have run out)."
        return super()._client_error_message(code, error)

    def container_logs(self, cluster: str, service: str) -> List[ContainerLog]:
        """The awslogs destinations of every container in the service's current task definition."""
        ecs = self._other("ecs")
        resp = self._call(ecs.describe_services, cluster=cluster, services=[service])
        found = [s for s in resp.get("services", []) if s.get("taskDefinition")]
        if not found:
            raise AwsError(f"Service '{service}' isn't in cluster '{cluster}' any more (the PR environment "
                           f"was probably torn down), so its log location can't be looked up.")
        task_def = self._call(ecs.describe_task_definition,
                              taskDefinition=found[0]["taskDefinition"])["taskDefinition"]
        result = []
        for container in task_def.get("containerDefinitions", []):
            log_cfg = container.get("logConfiguration") or {}
            options = log_cfg.get("options") or {}
            if log_cfg.get("logDriver") == "awslogs" and options.get("awslogs-group"):
                result.append(ContainerLog(container["name"], options["awslogs-group"],
                                           options.get("awslogs-stream-prefix", "")))
        if not result:
            raise AwsError(f"None of {service}'s containers send their logs to CloudWatch.")
        return result

    def get_lines(self, containers: List[ContainerLog], since: float,
                  filter_pattern: str = "") -> Tuple[List[LogLine], bool]:
        """Lines from the given containers since `since` (epoch seconds), oldest first. The second
        value is True if the result was cut short (page cap or MAX_EVENTS)."""
        logs = self._api()
        lines: List[LogLine] = []
        truncated = False
        for c in containers:
            kwargs = {"logGroupName": c.log_group, "startTime": int(since * 1000)}
            prefix = f"{c.stream_prefix}/{c.container}/" if c.stream_prefix else ""
            if prefix:
                kwargs["logStreamNamePrefix"] = prefix
            if filter_pattern:
                kwargs["filterPattern"] = filter_pattern
            token: Optional[str] = None
            for _ in range(MAX_PAGES):
                if token:
                    kwargs["nextToken"] = token
                resp = self._call(logs.filter_log_events, **kwargs)
                lines.extend(LogLine(e["timestamp"] / 1000, c.container, e.get("message", "").rstrip("\n"))
                             for e in resp.get("events", []))
                token = resp.get("nextToken")
                if not token:
                    break
            truncated = truncated or token is not None
        lines.sort(key=lambda line: line.timestamp)
        if len(lines) > MAX_EVENTS:
            lines, truncated = lines[-MAX_EVENTS:], True
        return lines, truncated
