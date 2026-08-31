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


@dataclass
class Location:
    name: str
    nrql: str
    enabled: bool = True
    use_custom_threshold: bool = False
    custom_threshold: float = 5.0

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
        settings = Settings(**data.get("settings", {}))
        return AppConfig(locations=locations, groups=groups, settings=settings)

    def save(self) -> None:
        APP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "locations": [asdict(l) for l in self.locations],
            "groups": [asdict(g) for g in self.groups],
            "settings": asdict(self.settings),
        }
        APP_CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
