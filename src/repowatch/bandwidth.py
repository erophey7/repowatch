"""Process-wide warm bandwidth, shared across threads and asyncio loops.

The application owns one budget through its StateStore. This is application
read pacing, not a traffic shaper for nginx or unrelated client requests.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
from pathlib import Path
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
TICK = 0.05
logger = logging.getLogger(__name__)


def validate_limit(value) -> None:
    if value is None:
        return
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('bandwidth limit must be a positive finite number or null')


def minute(value, *, end=False) -> int:
    if end and value == '24:00':
        return 1440
    if not isinstance(value, str) or len(value) != 5 or value[2] != ':':
        raise ValueError('schedule times must use HH:MM')
    hour, part = value[:2], value[3:]
    if not hour.isascii() or not part.isascii() or not hour.isdigit() or not part.isdigit():
        raise ValueError('schedule times must use HH:MM')
    if int(hour) > 23 or int(part) > 59:
        raise ValueError('invalid schedule time')
    return int(hour) * 60 + int(part)


def validate_schedule(schedule, zone: str) -> None:
    try:
        if not isinstance(zone, str):
            raise ValueError('bandwidth timezone must be an IANA timezone name')
        if zone != 'UTC':
            ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError('unknown bandwidth timezone; system timezone data is required') from exc
    if not isinstance(schedule, list) or len(schedule) > 64:
        raise ValueError('bandwidth schedule must be a list of at most 64 windows')
    occupied = set()
    for window in schedule:
        if not isinstance(window, dict) or set(window) - {'days', 'start', 'end', 'limit'}:
            raise ValueError('schedule window accepts only days/start/end/limit')
        if not {'start', 'end', 'limit'} <= set(window):
            raise ValueError('schedule window requires start, end and limit')
        validate_limit(window['limit'])
        days = window.get('days', list(DAYS))
        if not isinstance(days, list) or not days or any(day not in DAYS for day in days) or len(set(days)) != len(days):
            raise ValueError('schedule days must be unique mon/tue/wed/thu/fri/sat/sun values')
        start, end = minute(window['start']), minute(window['end'], end=True)
        if start == end:
            raise ValueError('schedule start and end must differ; use 00:00–24:00 for a full day')
        duration = (end - start) % 1440 or 1440
        for day in days:
            slots = {(DAYS.index(day) * 1440 + start + offset) % 10080 for offset in range(duration)}
            if occupied & slots:
                raise ValueError('bandwidth schedule windows overlap')
            occupied.update(slots)


def scheduled_limit(config, now: datetime | None = None) -> float | None:
    if not config.prefetch_bandwidth_schedule:
        return config.prefetch_bandwidth_limit
    zone = timezone.utc if config.prefetch_bandwidth_timezone == 'UTC' else ZoneInfo(config.prefetch_bandwidth_timezone)
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    position = local.weekday() * 1440 + local.hour * 60 + local.minute
    for window in config.prefetch_bandwidth_schedule:
        start, end = minute(window['start']), minute(window['end'], end=True)
        duration = (end - start) % 1440 or 1440
        for day in window.get('days', DAYS):
            if (position - (DAYS.index(day) * 1440 + start)) % 10080 < duration:
                return window['limit']
    return config.prefetch_bandwidth_limit


@dataclass(eq=False)
class _Request:
    repo_id: str
    remaining: float


class _Bucket:
    def __init__(self, now: float):
        self.updated = now
        self.rate = None
        self.credit = 0.0

    def refill(self, rate: float | None, now: float) -> None:
        if rate is None:
            self.credit = math.inf
        elif rate != self.rate:
            # Do not carry unlimited credit or old-rate debt into a new policy.
            self.credit = 0.0
        else:
            self.credit = min(rate * 0.1,
                              self.credit + max(0, now - self.updated) * rate)
        self.rate, self.updated = rate, now


class BandwidthBudget:
    """One shared global bucket and additional per-repository buckets.

    A short threading lock protects accounting only. Waiting uses each
    caller's own event loop. No lock is held over I/O or asyncio.sleep.
    Cancellation drops only the outstanding request; consumed credit is not
    refunded, since those bytes have already been read from the HTTP stream.
    """
    def __init__(self, default_limit: float | None = None):
        self._lock = threading.Lock()
        self._requests = deque()
        self._buckets = {}
        self._config = None
        self._default_limit = default_limit
        self._repo_limits = {}
        self._path = None
        self._stamp = None
        self._next_reload = 0.0

    def bind(self, path: str | Path) -> None:
        with self._lock:
            path = Path(path)
            if path != self._path:
                self._path, self._stamp, self._next_reload = path, None, 0.0

    def limiter(self, config, repo):
        with self._lock:
            if self._config is None or self._path is None:
                self._config = config
            self._repo_limits[repo.id] = repo.prefetch_bandwidth_limit
        return WarmBandwidthLimiter(self, repo.id)

    def _refresh(self) -> None:
        from repowatch.config import ConfigError, load_config
        with self._lock:
            now = time.monotonic()
            if self._path is None or now < self._next_reload:
                return
            self._next_reload = now + 1.0
            path = self._path
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            if stamp == self._stamp:
                return
            config = load_config(path)
        except (OSError, ConfigError, ValueError, yaml.YAMLError):
            logger.warning('bandwidth policy reload failed; retaining last valid settings')
            return
        with self._lock:
            if path == self._path:
                self._config, self._stamp = config, stamp

    def _step(self, now: float, wall: datetime | None = None) -> None:
        """Called with the accounting lock held; each eligible waiter gets a turn."""
        config = self._config
        global_rate = scheduled_limit(config, wall) if config is not None else self._default_limit
        rates = {None: global_rate}
        repo_rates = {repo.id: repo.prefetch_bandwidth_limit for repo in config.repos} if config is not None else {}
        for request in self._requests:
            rates[request.repo_id] = repo_rates.get(request.repo_id, self._repo_limits.get(request.repo_id))
        for key, rate in rates.items():
            self._buckets.setdefault(key, _Bucket(now)).refill(rate, now)
        for _ in range(len(self._requests)):
            request = self._requests.popleft()
            common, specific = self._buckets[None], self._buckets[request.repo_id]
            allowance = min(request.remaining, common.credit, specific.credit)
            request.remaining -= allowance
            common.credit -= allowance
            specific.credit -= allowance
            if request.remaining > 0:
                self._requests.append(request)
        self._requests.rotate(-1)
        # Keep the global bucket across operations; inactive repos do not leak
        # per-repo state indefinitely when configuration changes repeatedly.
        active = {request.repo_id for request in self._requests}
        for key in list(self._buckets):
            if key is not None and key not in active:
                # Retain recent credit across adjacent manual/automatic warms.
                if now - self._buckets[key].updated > 60:
                    del self._buckets[key]

    async def consume(self, repo_id: str, n_bytes: int) -> None:
        if n_bytes <= 0:
            return
        # Reload at most once per second, including during long-running warms.
        if self._path is not None and time.monotonic() >= self._next_reload:
            await asyncio.to_thread(self._refresh)
        request = _Request(repo_id, n_bytes)
        with self._lock:
            self._requests.append(request)
        try:
            while True:
                if self._path is not None and time.monotonic() >= self._next_reload:
                    await asyncio.to_thread(self._refresh)
                with self._lock:
                    self._step(time.monotonic())
                    if request.remaining <= 0:
                        return
                await asyncio.sleep(TICK)
        finally:
            with self._lock:
                if request in self._requests:
                    self._requests.remove(request)


class WarmBandwidthLimiter:
    def __init__(self, budget: BandwidthBudget, repo_id: str):
        self._budget, self._repo_id = budget, repo_id

    async def consume(self, n_bytes: int) -> None:
        await self._budget.consume(self._repo_id, n_bytes)


class BandwidthLimiter(WarmBandwidthLimiter):
    """Standalone compatibility wrapper; application warms use the shared owner."""
    def __init__(self, bytes_per_sec: float | None):
        validate_limit(bytes_per_sec)
        super().__init__(BandwidthBudget(bytes_per_sec), '')
