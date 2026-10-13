"""Расписание сбора: редкий полный обход + частый лёгкий цикл, с защитой от блокировок.

- RateLimiter — потолок загрузок страниц в час (общий для выдачи и карточек).
- Throttle — при капче или подозрительном ответе интервалы растут в разы и сами возвращаются к норме
  после нескольких чистых циклов.
- Scheduler — когда какой цикл запускать.
"""
from __future__ import annotations

import random
import time
from collections import deque


class RateLimiter:
    def __init__(self, max_per_hour: int = 45, clock=time.time, sleep=time.sleep):
        self.max_per_hour, self._clock, self._sleep = max(1, int(max_per_hour)), clock, sleep
        self._times: deque[float] = deque()

    def _purge(self) -> None:
        edge = self._clock() - 3600
        while self._times and self._times[0] <= edge:
            self._times.popleft()

    def used(self) -> int:
        self._purge()
        return len(self._times)

    def wait_time(self) -> float:
        """Сколько секунд ждать до следующей разрешённой загрузки (0 — можно сейчас)."""
        self._purge()
        if len(self._times) < self.max_per_hour:
            return 0.0
        return max(0.0, self._times[len(self._times) - self.max_per_hour] + 3600 - self._clock())

    def acquire(self) -> float:
        """Вызывается перед каждой загрузкой страницы: при превышении лимита ждёт. Возвращает, сколько ждали."""
        waited = self.wait_time()
        if waited > 0:
            self._sleep(waited)
        self._times.append(self._clock())
        return waited


class Throttle:
    """Множитель интервалов: 1 в норме, ×step при каждой неприятности (до cap), вдвое меньше после clean_needed
    чистых циклов подряд."""

    def __init__(self, step: float = 3.0, cap: float = 16.0, clean_needed: int = 3):
        self.step, self.cap, self.clean_needed = step, cap, clean_needed
        self.factor, self.clean, self.troubles = 1.0, 0, 0

    def report(self, trouble: bool) -> float:
        if trouble:
            self.factor = min(self.cap, self.factor * self.step)
            self.clean = 0
            self.troubles += 1
        elif self.factor > 1.0:
            self.clean += 1
            if self.clean >= self.clean_needed:
                self.factor = max(1.0, self.factor / 2)
                self.clean = 0
        return self.factor

    @property
    def slowed(self) -> bool:
        return self.factor > 1.0


class Scheduler:
    """Решает, какой цикл пора запускать. Полный обход важнее лёгкого; пауза ставит оба на удержание."""

    def __init__(self, light_min: float = 180, light_max: float = 300, full_interval: float = 3600, light: bool = True,
                 clock=time.time, rng=random.uniform):
        self.light_min, self.light_max, self.full_interval, self.light = light_min, light_max, full_interval, light
        self._clock, self._rng = clock, rng
        self.throttle = Throttle()
        self.next_full_at = 0.0                          # первый полный обход — сразу после старта
        self.next_light_at = clock() + rng(light_min, light_max)
        self.last: dict = {}

    def configure(self, light_min: float, light_max: float, full_interval: float, light: bool) -> None:
        self.light_min, self.light_max = max(30.0, light_min), max(light_min, light_max)
        self.full_interval, self.light = max(300.0, full_interval), light

    def due(self) -> str | None:
        now = self._clock()
        if now >= self.next_full_at:
            return "full"
        if self.light and now >= self.next_light_at:
            return "light"
        return None

    def next_at(self) -> float:
        return min(self.next_full_at, self.next_light_at) if self.light else self.next_full_at

    def force(self, kind: str = "full") -> None:
        if kind == "full":
            self.next_full_at = 0.0
        else:
            self.next_light_at = 0.0

    def done(self, kind: str, trouble: bool) -> None:
        """Цикл завершён: пересчитать защиту и назначить следующий запуск."""
        now = self._clock()
        factor = self.throttle.report(trouble)
        if kind == "full":
            self.next_full_at = now + self.full_interval * self._rng(0.9, 1.2) * (factor if trouble else 1.0)
            self.next_light_at = max(self.next_light_at, now + self._rng(self.light_min, self.light_max) * factor)
        else:
            self.next_light_at = now + self._rng(self.light_min, self.light_max) * factor
            if trouble:                                  # неприятность в лёгком цикле отодвигает и полный обход
                self.next_full_at = max(self.next_full_at, now + self.light_max * factor)
        self.last = {"kind": kind, "trouble": trouble, "at": now, "factor": factor}

    def status(self) -> dict:
        return {"factor": self.throttle.factor, "slowed": self.throttle.slowed, "troubles": self.throttle.troubles,
                "next_full_at": self.next_full_at, "next_light_at": self.next_light_at if self.light else None,
                "light": self.light, "last": self.last}


def is_trouble(rc: int, info: dict) -> bool:
    """Капча, блокировка, ошибка загрузки или пустая выдача там, где она должна быть, — повод замедлиться."""
    return rc in (3, 5) or bool(info.get("captcha")) or bool(info.get("blocked")) or bool(info.get("empty"))


def dump_state(sched: Scheduler, limiter: RateLimiter) -> dict:
    return {"times": list(limiter._times), "factor": sched.throttle.factor, "clean": sched.throttle.clean,
            "troubles": sched.throttle.troubles, "next_full_at": sched.next_full_at, "next_light_at": sched.next_light_at}


def load_state(sched: Scheduler, limiter: RateLimiter, state: dict) -> None:
    """Вернуть состояние после перезапуска. Полный обход не начнётся раньше назначенного времени,
    а запросы за последний час продолжают считаться."""
    if not state:
        return
    now = limiter._clock()
    limiter._times = deque(sorted(t for t in state.get("times", []) if isinstance(t, (int, float)) and now - 3600 < t <= now))
    sched.throttle.factor = min(sched.throttle.cap, max(1.0, float(state.get("factor", 1.0))))
    sched.throttle.clean = int(state.get("clean", 0))
    sched.throttle.troubles = int(state.get("troubles", 0))
    if state.get("next_full_at"):
        sched.next_full_at = float(state["next_full_at"])
    if state.get("next_light_at"):
        sched.next_light_at = max(float(state["next_light_at"]), now + 20)   # не сразу после старта
