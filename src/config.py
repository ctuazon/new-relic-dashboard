"""Application configuration: environment secrets and persisted app settings."""
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
APP_CONFIG_PATH = BASE_DIR / "config" / "app_config.json"
LOG_DIR = BASE_DIR / "logs"

load_dotenv(ENV_PATH)

NEW_RELIC_API_KEY = os.getenv("NEW_RELIC_API_KEY", "").strip()
NEW_RELIC_ACCOUNT_ID = os.getenv("NEW_RELIC_ACCOUNT_ID", "").strip()
NEW_RELIC_REGION = os.getenv("NEW_RELIC_REGION", "US").strip().upper()

GRAPHQL_ENDPOINTS = {
    "US": "https://api.newrelic.com/graphql",
    "EU": "https://api.eu.newrelic.com/graphql",
}


_APP_NAME_RE = re.compile(r"appName\s*=\s*'([^']+)'")
_SINCE_RE = re.compile(r"SINCE\s+(\d+)\s+(second|minute|hour|day)s?\s+ago", re.IGNORECASE)
_SINCE_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
_DEFAULT_LOOKBACK_SECONDS = 300

# Any Monolog record at ERROR or above, plus PHP fatals (which bypass Monolog). Plain
# "[error]" is useless here: nginx tags every PHP message that way, NOTICEs included.
DEFAULT_CLOUDWATCH_FILTER = (
    '?"marketplace.ERROR" ?"marketplace.CRITICAL" ?"marketplace.ALERT" '
    '?"marketplace.EMERGENCY" ?"PHP Fatal"'
)


@dataclass
class Location:
    name: str
    nrql: str
    enabled: bool = True
    use_custom_threshold: bool = False
    custom_threshold: float = 5.0
    # Site hostname this realm's lines carry in the shared CloudWatch log group (nginx's
    # host: "..."). None derives it from the name, e.g. 'RVTrader (ECS)' -> www.rvtrader.com.
    log_host: Optional[str] = None

    @property
    def cloudwatch_host(self) -> str:
        if self.log_host:
            return self.log_host.strip().lower()
        base = self.name.split("(")[0].strip().replace(" ", "").lower()
        return f"www.{base}.com"

    @property
    def app_name(self) -> str:
        """The appName this location's NRQL actually filters on, falling back to the
        display name if the query doesn't filter on appName directly."""
        match = _APP_NAME_RE.search(self.nrql)
        return match.group(1) if match else self.name

    @property
    def lookback_seconds(self) -> int:
        """The SINCE window this location's NRQL actually queries, so the transaction-volume
        guard checks the same window the alerting value was computed over. Falls back to a
        default if the query doesn't use a plain 'SINCE N <unit> ago' clause."""
        match = _SINCE_RE.search(self.nrql)
        if not match:
            return _DEFAULT_LOOKBACK_SECONDS
        return int(match.group(1)) * _SINCE_UNIT_SECONDS[match.group(2).lower()]


@dataclass
class Group:
    """A named combination of locations (or a hand-written NRQL) monitored as one series."""
    name: str
    members: List[str] = field(default_factory=list)
    custom_nrql: Optional[str] = None
    use_custom_threshold: bool = False
    custom_threshold: float = 5.0


DEV_AWS_PROFILE = "MarketplaceDevs-toldev-729320268350"  # where the PR environments run


@dataclass
class ChangeTarget:
    """An ECS cluster (optionally narrowed to services whose name starts with a prefix) whose
    deploys and changes are pulled from CloudTrail into the CloudTrail tab. A cluster ending in *
    matches every cluster with that prefix, for PR environments that get a cluster each."""
    label: str
    cluster: str
    service_prefix: str = ""
    profile: str = "xforce"
    region: str = "us-east-1"
    show_on_charts: bool = True  # prod deploys line up with the prod charts; PR deploys don't

    @property
    def is_pattern(self) -> bool:
        return self.cluster.endswith("*")

    def matches_cluster(self, cluster: str) -> bool:
        if self.is_pattern:
            return cluster.startswith(self.cluster[:-1])
        return cluster == self.cluster

    def matches(self, cluster: str, service: str) -> bool:
        return self.matches_cluster(cluster) and service.startswith(self.service_prefix)


def default_change_targets() -> List["ChangeTarget"]:
    return [
        ChangeTarget("Marketplace", "marketplace-prod"),
        ChangeTarget("SSR", "marketplace-ssr-prod"),  # includes the -preview service
        ChangeTarget("Search API", "marketplace-search-api-prod"),
        ChangeTarget("SSR PRs", "marketplace-ssr-pr", profile=DEV_AWS_PROFILE, show_on_charts=False),
        ChangeTarget("Search API PRs", "mp-sapi-pr-env-*", profile=DEV_AWS_PROFILE, show_on_charts=False),
    ]


@dataclass
class Settings:
    poll_interval_seconds: int = 60
    threshold_stdev_multiplier: float = 2.0
    min_samples_for_auto_threshold: int = 5
    flat_fallback_threshold_percent: float = 5.0
    sound_enabled: bool = True
    tts_enabled: bool = True
    alert_repeat_seconds: int = 8
    error_inbox_enabled: bool = True
    error_inbox_lookback_seconds: int = 3600
    error_inbox_container_attribute: str = "containerId"
    min_transactions_for_alert: int = 20
    mini_view_x: Optional[int] = None
    mini_view_y: Optional[int] = None
    theme: Optional[str] = None  # "light" / "dark"; None follows the Windows app theme
    cloudwatch_enabled: bool = True
    cloudwatch_aws_profile: str = "xforce"  # an AWS SSO profile; log in with `aws sso login`
    cloudwatch_region: str = "us-east-1"
    cloudwatch_log_group: str = "marketplace-prod-logs"
    cloudwatch_filter_pattern: str = DEFAULT_CLOUDWATCH_FILTER
    # Substrings of known-noise lines to drop before counting, e.g. a repeating bug already ticketed.
    cloudwatch_exclude_patterns: List[str] = field(default_factory=list)
    cloudwatch_lookback_seconds: int = 300
    cloudwatch_poll_interval_seconds: int = 60
    min_log_errors_for_alert: int = 10
    # Error-line count threshold used until a realm has enough history for its auto threshold.
    cloudwatch_fallback_threshold: float = 100.0
    cloudtrail_enabled: bool = True
    cloudtrail_targets: List[ChangeTarget] = field(default_factory=default_change_targets)
    cloudtrail_lookback_hours: int = 168  # a week, so the last deploy is usually in view
    cloudtrail_poll_interval_seconds: int = 120
    cloudtrail_show_autoscaling: bool = False  # Auto Scaling resizes services many times a day
    chart_legend_visible: bool = True  # hidden, the hover tooltip still names every series


@dataclass
class AppConfig:
    locations: List[Location] = field(default_factory=list)
    groups: List[Group] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)

    @staticmethod
    def load() -> "AppConfig":
        if not APP_CONFIG_PATH.exists():
            cfg = AppConfig()
            cfg.groups.append(Group(name="All Locations Combined", members=[]))
            cfg.save()
            return cfg
        data = json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
        locations = [Location(**l) for l in data.get("locations", [])]
        groups = [Group(**g) for g in data.get("groups", [])]
        settings_data = dict(data.get("settings", {}))
        if "cloudtrail_targets" in settings_data:
            settings_data["cloudtrail_targets"] = [ChangeTarget(**t) for t in settings_data["cloudtrail_targets"]]
        settings = Settings(**settings_data)
        return AppConfig(locations=locations, groups=groups, settings=settings)

    def save(self) -> None:
        APP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "locations": [asdict(l) for l in self.locations],
            "groups": [asdict(g) for g in self.groups],
            "settings": asdict(self.settings),
        }
        APP_CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
