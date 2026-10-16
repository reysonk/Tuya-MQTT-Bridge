import threading
import time
from typing import Callable, Dict, List, Optional, Tuple


class CommandTracker:
    def __init__(
        self,
        ttl_sec: float,
        retry_at_sec: float,
        retry_backoff: float,
        retry_max: int,
        avg_sec_fn: Optional[Callable[[str], float]] = None,
    ):
        self._ttl_sec = ttl_sec
        self._retry_at_sec = retry_at_sec
        self._retry_backoff = retry_backoff
        self._retry_max = retry_max
        self._avg_sec_fn = avg_sec_fn
        self._seq = 0
        self._records: Dict[Tuple[str, str], dict] = {}
        self._lock = threading.RLock()
        self._now_fn: Callable[[], float] = time.time

    def set_now_fn(self, now_fn: Callable[[], float]) -> None:
        """Inject time function for testing."""
        self._now_fn = now_fn

    def _now(self) -> float:
        return self._now_fn()

    def _retry_interval(self, attempts: int, dev_name: str) -> float:
        base = self._retry_at_sec
        if self._avg_sec_fn is not None:
            avg = self._avg_sec_fn(dev_name)
            base = max(base, 2.0 * avg)
        return base * (self._retry_backoff ** attempts)

    def offer(self, dev_name: str, dp: str, value, source: str = "set") -> Optional[int]:
        key = (dev_name, dp)
        now = self._now()
        with self._lock:
            rec = self._records.get(key)
            if rec is not None and rec["desired"] == value and now < rec["deadline"]:
                return None
            self._seq += 1
            gen = self._seq
            rec = {
                "desired": value,
                "gen": gen,
                "sent_at": now,
                "last_send_at": now,
                "attempts": 0,
                "deadline": now + self._ttl_sec,
                "source": source,
            }
            self._records[key] = rec
            return gen

    def confirm(self, dev_name: str, dp: str, value) -> bool:
        key = (dev_name, dp)
        now = self._now()
        with self._lock:
            rec = self._records.get(key)
            if rec is not None and rec["desired"] == value:
                del self._records[key]
                return True
            return False

    def drop(self, dev_name: str, dp: str) -> bool:
        """Снять запись по паре (подтверждение/сброс). True — запись была."""
        key = (dev_name, dp)
        with self._lock:
            return self._records.pop(key, None) is not None

    def snapshot(self, dev_name: str, dp: str) -> Optional[dict]:
        key = (dev_name, dp)
        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                return None
            return rec.copy()

    def due(self, now: Optional[float] = None) -> List[Tuple[str, str, int, int]]:
        if now is None:
            now = self._now()
        result: List[Tuple[str, str, int, int]] = []
        with self._lock:
            for (dev_name, dp), rec in self._records.items():
                if rec["attempts"] >= self._retry_max:
                    continue
                if now >= rec["deadline"]:
                    continue
                interval = self._retry_interval(rec["attempts"], dev_name)
                if now - rec["last_send_at"] >= interval:
                    result.append((dev_name, dp, rec["desired"], rec["gen"], rec["attempts"]))
        return result

    def mark_sent(self, dev_name: str, dp: str, gen: int) -> bool:
        key = (dev_name, dp)
        now = self._now()
        with self._lock:
            rec = self._records.get(key)
            if rec is None or rec["gen"] != gen:
                return False
            rec["last_send_at"] = now
            rec["attempts"] += 1
            return True

    def expire(self, now: Optional[float] = None) -> List[str]:
        if now is None:
            now = self._now()
        expired_devs: List[str] = []
        with self._lock:
            to_delete: List[Tuple[str, str]] = []
            for (dev_name, dp), rec in self._records.items():
                if now >= rec["deadline"]:
                    to_delete.append((dev_name, dp))
            for dev_name, dp in to_delete:
                del self._records[(dev_name, dp)]
            # Build list of devices that now have no pending records
            devices_with_records = set(dev_name for dev_name, _ in self._records.keys())
            all_devs = set(dev_name for dev_name, _ in to_delete)
            expired_devs = [dev_name for dev_name in all_devs if dev_name not in devices_with_records]
        return expired_devs

    def pending_any(self, dev_name: str) -> bool:
        now = self._now()
        with self._lock:
            for (d, _), rec in self._records.items():
                if d == dev_name and now < rec["deadline"]:
                    return True
            return False

    def pending_map(self, dev_name: str) -> Dict[str, any]:
        now = self._now()
        result: Dict[str, any] = {}
        with self._lock:
            for (d, dp), rec in self._records.items():
                if d == dev_name and now < rec["deadline"]:
                    result[dp] = rec["desired"]
        return result

    def stats(self) -> dict:
        with self._lock:
            total = len(self._records)
            by_dev: Dict[str, int] = {}
            for (dev_name, _), rec in self._records.items():
                by_dev[dev_name] = by_dev.get(dev_name, 0) + 1
            return {
                "total": total,
                "by_dev": by_dev,
                "seq": self._seq,
            }

    def clear(self) -> None:
        with self._lock:
            self._records.clear()