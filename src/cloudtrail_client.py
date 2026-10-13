"""CloudTrail feed of ECS deploys and changes (who deployed, scaled, created, deleted or
stopped what, and when) for the Marketplace clusters."""
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from .aws import AwsClient

# Looked up by cluster name (CloudTrail's ResourceName), which returns just the calls that touched
# that cluster -- a few hundred a week -- instead of the account's thousands of ECS calls an hour.
ECS_EVENT_SOURCE = "ecs.amazonaws.com"
LOOKUP_PAGE_SIZE = 50  # LookupEvents' maximum
# Some cluster names are shared with log groups, whose (read-only, dropped) calls also come back:
# the SSR PR cluster returns ~4k events a week for ~300 real changes.
MAX_PAGES_PER_CLUSTER = 120
# Per-PR clusters come and go, so their creation/teardown is looked up by event name.
CLUSTER_LIFECYCLE_EVENTS = ("CreateCluster", "DeleteCluster")
LOOKUP_PACING_SECONDS = 0.6  # LookupEvents allows 2 requests/second per account and region
AUTOSCALING_ROLE_MARKER = "AutoScaling"
# PR environments carry the PR number in their name: marketplace-ssr-pr3384 (an SSR PR service),
# mp-sapi-pr-env-1096 / mp-sapi-pr-env-1096-svc (a Search API PR cluster). "prod" never matches.
_PR_RE = re.compile(r"(?:^|-)pr(?:-env)?-?(\d+)")


@dataclass
class ChangeEvent:
    event_id: str
    timestamp: float  # epoch seconds
    event_name: str
    kind: str  # "deploy", "scale", "create", "delete", "stop", "update" or "failed"
    cluster: str
    service: str
    summary: str
    actor: str
    role: str
    raw: str  # the full CloudTrail record, pretty-printed
    target: str = ""  # label of the configured target it matched
    on_charts: bool = True  # whether the target wants deploy markers on the dashboard charts

    @property
    def is_autoscaling(self) -> bool:
        return AUTOSCALING_ROLE_MARKER in self.role

    @property
    def pr_number(self) -> str:
        """The PR this change belongs to ("" for prod). PR numbers are per repo, so SSR #1096
        and Search API #1096 are different PRs; the App column tells them apart."""
        match = _PR_RE.search(self.service) or _PR_RE.search(self.cluster)
        return match.group(1) if match else ""


def _basename(arn_or_name: Optional[str]) -> str:
    return (arn_or_name or "").rsplit("/", 1)[-1]


def _actor(identity: dict):
    """(who, role): for an assumed role the session name is the useful 'who' -- a person's email,
    'GitHubActions-MarketplaceSSR', a Jenkins job -- and the role says what kind of caller it is."""
    arn = identity.get("arn") or ""
    if identity.get("type") == "AssumedRole" and arn.count("/") >= 2:
        _prefix, role, session = arn.split("/", 2)
        if AUTOSCALING_ROLE_MARKER in role:
            return "Auto Scaling", role
        return session, role
    if identity.get("invokedBy"):
        return identity["invokedBy"], identity.get("type", "")
    return _basename(arn) or identity.get("type", "unknown"), identity.get("type", "")


def parse_event(lookup_event: dict, record: Optional[dict] = None) -> ChangeEvent:
    record = record if record is not None else json.loads(lookup_event.get("CloudTrailEvent") or "{}")
    params = record.get("requestParameters") or {}
    name = lookup_event.get("EventName") or record.get("eventName") or ""
    actor, role = _actor(record.get("userIdentity") or {})
    service = _basename(params.get("service") or params.get("serviceName"))

    if name == "UpdateService":
        if params.get("taskDefinition"):
            kind, summary = "deploy", f"Deployed {_basename(params['taskDefinition'])}"
        elif params.get("forceNewDeployment"):
            kind, summary = "deploy", "Redeployed (force new deployment)"
        elif params.get("desiredCount") is not None:
            kind, summary = "scale", f"Scaled to {params['desiredCount']} tasks"
        else:
            kind, summary = "update", "Updated service settings"
    elif name == "CreateService":
        kind, summary = "create", f"Created service ({_basename(params.get('taskDefinition')) or 'no task def'})"
    elif name == "DeleteService":
        kind, summary = "delete", "Deleted service"
    elif name == "CreateCluster":
        kind, summary = "create", "Created cluster (environment started)"
    elif name == "DeleteCluster":
        kind, summary = "delete", "Deleted cluster (environment torn down)"
    elif name == "StopTask":
        task = _basename(params.get("task"))[:12]
        reason = params.get("reason")
        kind, summary = "stop", f"Stopped task {task}" + (f" ({reason})" if reason else "")
    else:
        kind, summary = "update", name

    if record.get("errorCode"):
        kind = "failed"
        summary = f"FAILED {summary.lower()}: {record['errorCode']}"

    when = lookup_event.get("EventTime")
    timestamp = when.timestamp() if isinstance(when, datetime) else time.time()
    return ChangeEvent(
        event_id=lookup_event.get("EventId") or record.get("eventID") or "",
        timestamp=timestamp, event_name=name, kind=kind,
        cluster=_basename(params.get("cluster") or params.get("clusterName")) or "default", service=service,
        summary=summary, actor=actor, role=role, raw=json.dumps(record, indent=2, default=str),
    )


class CloudTrailClient(AwsClient):
    service = "cloudtrail"
    label = "CloudTrail"

    def __init__(self, profile: str, region: str):
        super().__init__(profile, region)
        self._account_id: Optional[str] = None

    def _cluster_resource_names(self, cluster: str) -> List[str]:
        """CloudTrail files an event under the cluster exactly as the caller named it: GitHub
        Actions passes the short name, Jenkins the full ARN, so both have to be looked up."""
        if self._account_id is None:
            self._account_id = self._call(self._other("sts").get_caller_identity)["Account"]
        return [cluster, f"arn:aws:ecs:{self.region}:{self._account_id}:cluster/{cluster}"]

    def _probe(self) -> str:
        self._call(self._api().lookup_events, MaxResults=1)
        return "reachable."

    def _existing_clusters(self, prefix: str) -> List[str]:
        names: List[str] = []
        kwargs = {}
        while True:
            resp = self._call(self._other("ecs").list_clusters, **kwargs)
            names += [_basename(arn) for arn in resp.get("clusterArns", [])]
            if not resp.get("nextToken"):
                break
            kwargs["nextToken"] = resp["nextToken"]
        return [n for n in names if n.startswith(prefix)]

    def get_changes(self, clusters: List[str], start: float, end: Optional[float] = None) -> List[ChangeEvent]:
        """ECS write events (deploys, scaling, create/delete, task stops) on the given clusters
        between start and end (epoch seconds), newest first. A name ending in * covers every
        cluster with that prefix: the ones that exist now are looked up, and cluster creation/
        teardown events catch the ones already gone. Read-only calls and other services acting
        on the cluster (e.g. Auto Scaling lifecycle hooks) are dropped."""
        api = self._api()
        start_dt = datetime.fromtimestamp(start, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(end or time.time(), tz=timezone.utc)
        events = {}  # by event id: an event can name the cluster both ways
        exact = [c for c in clusters if not c.endswith("*")]
        prefixes = [c[:-1] for c in clusters if c.endswith("*")]
        for prefix in prefixes:
            exact += self._existing_clusters(prefix)
        lookups = [("ResourceName", n) for cluster in dict.fromkeys(exact)
                   for n in self._cluster_resource_names(cluster)]
        if prefixes:
            lookups += [("EventName", n) for n in CLUSTER_LIFECYCLE_EVENTS]

        first = True
        for key, value in lookups:
            kwargs = {
                "LookupAttributes": [{"AttributeKey": key, "AttributeValue": value}],
                "StartTime": start_dt, "EndTime": end_dt, "MaxResults": LOOKUP_PAGE_SIZE,
            }
            for _page in range(MAX_PAGES_PER_CLUSTER):
                if not first:
                    time.sleep(LOOKUP_PACING_SECONDS)
                first = False
                resp = self._call(api.lookup_events, **kwargs)
                for e in resp.get("Events", []):
                    record = json.loads(e.get("CloudTrailEvent") or "{}")
                    if record.get("eventSource") == ECS_EVENT_SOURCE and not record.get("readOnly"):
                        event = parse_event(e, record)
                        events[event.event_id] = event
                token = resp.get("NextToken")
                if not token:
                    break
                kwargs["NextToken"] = token
        return sorted(events.values(), key=lambda e: e.timestamp, reverse=True)
