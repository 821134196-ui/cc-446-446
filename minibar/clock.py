"""可注入的时钟，便于测试复现。"""
from datetime import datetime, date


class Clock:
    def __init__(self):
        self._fixed = None

    def set_fixed(self, dt):
        self._fixed = dt if isinstance(dt, datetime) else datetime.fromisoformat(dt)

    def reset(self):
        self._fixed = None

    def now(self):
        return self._fixed or datetime.now()

    def today(self):
        return self._fixed.date() if self._fixed else date.today()

    def iso(self):
        return self.now().isoformat(timespec="seconds")


clock = Clock()


def days_between(d_iso, today):
    return (date.fromisoformat(d_iso) - today).days
