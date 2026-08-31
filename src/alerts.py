"""Audible + spoken anomaly alerts that repeat until acknowledged (the 'red light' escalation)."""
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

try:
    import pyttsx3
    HAS_TTS = True
except ImportError:
    HAS_TTS = False


@dataclass
class Alert:
    key: str
    name: str
    value: float
    threshold: float
    started_at: float
    acknowledged: bool = False


class _TtsWorker:
    """Serializes pyttsx3 calls on one background thread since the engine isn't thread-safe."""

    def __init__(self):
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._engine = None
        if HAS_TTS:
            try:
                self._engine = pyttsx3.init()
            except Exception:
                self._engine = None
        threading.Thread(target=self._run, daemon=True).start()

    def say(self, text: str) -> None:
        if self._engine is not None:
            self._queue.put(text)

    def _run(self):
        while True:
            text = self._queue.get()
            try:
                self._engine.say(text)
                self._engine.runAndWait()
            except Exception:
                pass


class AlertManager:
    """Tracks active anomalies per series and loops a beep + spoken location name
    until the user clicks OK for that specific alert."""

    def __init__(self, on_change: Callable[[], None], sound_enabled: bool = True,
                 tts_enabled: bool = True, repeat_seconds: int = 8):
        self.sound_enabled = sound_enabled
        self.tts_enabled = tts_enabled
        self.repeat_seconds = repeat_seconds
        self._alerts: Dict[str, Alert] = {}
        self._lock = threading.Lock()
        self._on_change = on_change
        self._tts = _TtsWorker()
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def raise_alert(self, key: str, name: str, value: float, threshold: float) -> None:
        with self._lock:
            existing = self._alerts.get(key)
            if existing and not existing.acknowledged:
                existing.value = value
                existing.threshold = threshold
                return
            self._alerts[key] = Alert(key, name, value, threshold, time.time())
        self._on_change()

    def clear_alert(self, key: str) -> None:
        with self._lock:
            self._alerts.pop(key, None)
        self._on_change()

    def acknowledge(self, key: str) -> None:
        with self._lock:
            alert = self._alerts.get(key)
            if alert:
                alert.acknowledged = True
        self._on_change()

    def acknowledge_all(self) -> None:
        with self._lock:
            for alert in self._alerts.values():
                alert.acknowledged = True
        self._on_change()

    def active_alerts(self):
        with self._lock:
            return list(self._alerts.values())

    def _loop(self):
        while not self._stop.is_set():
            unacknowledged = [a for a in self.active_alerts() if not a.acknowledged]
            for alert in unacknowledged:
                if self.sound_enabled and HAS_WINSOUND:
                    try:
                        winsound.Beep(1000, 350)
                        winsound.Beep(1300, 350)
                    except Exception:
                        pass
                if self.tts_enabled:
                    self._tts.say(f"Anomaly detected in {alert.name}. Error rate {alert.value:.1f} percent.")
                time.sleep(0.4)
            self._stop.wait(self.repeat_seconds if unacknowledged else 1.0)

    def shutdown(self) -> None:
        self._stop.set()
