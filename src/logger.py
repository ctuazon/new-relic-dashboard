"""Session logging: every poll and anomaly event is timestamped and written to disk."""
import csv
import datetime as dt
import threading

from . import config


class SessionLogger:
    def __init__(self):
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = config.LOG_DIR / f"session_{stamp}.log"
        self.csv_path = config.LOG_DIR / f"session_{stamp}_readings.csv"
        self._lock = threading.Lock()
        self.event("INFO", f"Session started. Logging to {self.log_path.name}")

    @staticmethod
    def _timestamp() -> str:
        return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def event(self, level: str, message: str) -> None:
        line = f"[{self._timestamp()}] {level:<7} {message}\n"
        with self._lock:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line)

    def reading(self, name: str, value: float, threshold: float, status: str) -> None:
        with self._lock:
            is_new = not self.csv_path.exists()
            with self.csv_path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if is_new:
                    writer.writerow(["date", "time", "name", "error_rate", "threshold", "status"])
                now = dt.datetime.now()
                writer.writerow([
                    now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
                    name, f"{value:.4f}", f"{threshold:.4f}", status,
                ])

    def tail(self, max_lines: int = 300):
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        return lines[-max_lines:]
