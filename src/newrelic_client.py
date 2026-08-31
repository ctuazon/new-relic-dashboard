"""Thin client around the New Relic NerdGraph (GraphQL) API."""
from typing import Any, Optional, Tuple

import requests

from . import config

TIMEOUT = 20

# The 4 classic APM overview charts. Each entry is (key, chart title, NRQL SELECT clause).
OVERVIEW_METRICS = [
    ("response_time", "Web Transactions Time (s)", "average(duration)"),
    ("apdex", "Apdex Score", "apdex(duration)"),
    ("throughput", "Throughput (rpm)", "rate(count(*), 1 minute)"),
    ("errors", "Errors (%)", "percentage(count(*), WHERE error IS true)"),
]


class NewRelicError(Exception):
    pass


class NewRelicClient:
    def __init__(self, api_key: Optional[str] = None, account_id: Optional[str] = None,
                 region: Optional[str] = None):
        self.api_key = api_key or config.NEW_RELIC_API_KEY
        self.account_id = account_id or config.NEW_RELIC_ACCOUNT_ID
        self.region = (region or config.NEW_RELIC_REGION or "US").upper()
        self.endpoint = config.GRAPHQL_ENDPOINTS.get(self.region, config.GRAPHQL_ENDPOINTS["US"])

    def _post(self, query: str, variables: Optional[dict] = None) -> dict:
        if not self.api_key:
            raise NewRelicError("No New Relic API key configured. Add it to your .env file.")
        headers = {"Content-Type": "application/json", "Api-Key": self.api_key}
        payload = {"query": query, "variables": variables or {}}
        resp = requests.post(self.endpoint, json=payload, headers=headers, timeout=TIMEOUT)
        if resp.status_code == 401:
            raise NewRelicError("New Relic rejected the API key (401 Unauthorized).")
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise NewRelicError("; ".join(e.get("message", str(e)) for e in body["errors"]))
        return body.get("data", {})

    def test_connection(self) -> Tuple[bool, str]:
        """Validates the API key, and if an account ID is set, confirms NRQL access to it."""
        try:
            data = self._post("{ actor { user { name email } } }")
            user = (data.get("actor") or {}).get("user") or {}
            name = user.get("name") or user.get("email") or "unknown user"
            if not self.account_id:
                return True, f"Connected as {name}. Warning: NEW_RELIC_ACCOUNT_ID is not set."
            self.run_nrql("SELECT count(*) FROM Transaction SINCE 1 minute ago")
            return True, f"Connected as {name}. Account {self.account_id} is reachable."
        except NewRelicError as e:
            return False, str(e)
        except requests.RequestException as e:
            return False, f"Network error: {e}"

    def run_nrql(self, nrql: str) -> Any:
        """Runs an NRQL query against the configured account and returns the first result row."""
        if not self.account_id:
            raise NewRelicError("NEW_RELIC_ACCOUNT_ID is not set in .env")
        query = """
        query($accountId: Int!, $nrql: Nrql!) {
          actor {
            account(id: $accountId) {
              nrql(query: $nrql) {
                results
              }
            }
          }
        }
        """
        data = self._post(query, {"accountId": int(self.account_id), "nrql": nrql})
        account = (data.get("actor") or {}).get("account") or {}
        nrql_result = account.get("nrql") or {}
        results = nrql_result.get("results") or []
        if not results:
            raise NewRelicError("NRQL query returned no results.")
        return results[0]

    def run_nrql_facets(self, nrql: str) -> list:
        """Runs any NRQL query and returns all result rows (not just the first). Works for
        FACET queries (each row keyed by a positional 'facet' list) and plain raw-event
        SELECTs (each row keyed by the selected attribute names) alike."""
        if not self.account_id:
            raise NewRelicError("NEW_RELIC_ACCOUNT_ID is not set in .env")
        query = """
        query($accountId: Int!, $nrql: Nrql!) {
          actor {
            account(id: $accountId) {
              nrql(query: $nrql) {
                results
              }
            }
          }
        }
        """
        data = self._post(query, {"accountId": int(self.account_id), "nrql": nrql})
        account = (data.get("actor") or {}).get("account") or {}
        nrql_result = account.get("nrql") or {}
        return nrql_result.get("results") or []

    def get_error_events(self, app_name: str, lookback_seconds: int, container_attribute: str,
                          limit: int = 100) -> list:
        """Errors Inbox source data: individual TransactionError occurrences for an app over
        the lookback window, newest first, with per-occurrence detail (endpoint, method,
        status code, container, guid) rather than aggregated counts."""
        nrql = (
            f"SELECT timestamp, error.class, error.message, request.uri, request.method, "
            f"http.statusCode, {container_attribute}, guid, transactionName "
            f"FROM TransactionError WHERE appName = '{app_name}' "
            f"SINCE {int(lookback_seconds)} seconds ago ORDER BY timestamp DESC LIMIT {int(limit)}"
        )
        return self.run_nrql_facets(nrql)

    def get_transaction_count(self, app_name: str, lookback_seconds: int) -> float:
        """Total Transaction volume for an app over a window — a sample-size guard so a
        handful of requests right after deploy can't swing a percentage metric like errorRate
        into a false anomaly."""
        nrql = (
            f"SELECT count(*) FROM Transaction WHERE appName = '{app_name}' "
            f"SINCE {int(lookback_seconds)} seconds ago"
        )
        return self.run_nrql_value(nrql)

    def get_overview_timeseries(self, app_names: list, lookback_minutes: int = 60,
                                 bucket_minutes: int = 1) -> dict:
        """Fetches the 4 APM overview series (response time, apdex, throughput, error rate)
        for a set of apps, one NRQL call per metric faceted by appName — so the query count
        stays at 4 regardless of how many apps are being watched. Returns
        {metric_key: {app_name: [(epoch_seconds, value), ...]}}."""
        if not app_names:
            return {key: {} for key, _, _ in OVERVIEW_METRICS}
        in_clause = ", ".join(f"'{a}'" for a in app_names)
        result = {}
        for key, _title, select_clause in OVERVIEW_METRICS:
            nrql = (
                f"SELECT {select_clause} AS 'value' FROM Transaction "
                f"WHERE appName IN ({in_clause}) SINCE {int(lookback_minutes)} minutes ago "
                f"TIMESERIES {int(bucket_minutes)} minute FACET appName"
            )
            rows = self.run_nrql_facets(nrql)
            result[key] = self._parse_timeseries(rows)
        return result

    @staticmethod
    def _parse_timeseries(rows: list) -> dict:
        series: dict = {}
        for row in rows:
            app = row.get("appName")
            t = row.get("beginTimeSeconds")
            val = row.get("value")
            if isinstance(val, dict):  # apdex returns a nested {count, f, s, score, t}
                val = val.get("score")
            if app is None or t is None or val is None:
                continue
            series.setdefault(app, []).append((t, val))
        for points in series.values():
            points.sort(key=lambda p: p[0])
        return series

    def run_nrql_value(self, nrql: str) -> float:
        """Runs an NRQL query expected to return a single numeric column and returns that number."""
        row = self.run_nrql(nrql)
        for value in row.values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        raise NewRelicError(f"NRQL result did not contain a numeric value: {row}")
