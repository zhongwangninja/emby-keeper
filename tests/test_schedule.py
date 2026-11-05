import asyncio
from datetime import datetime, time, timedelta
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import embykeeper.cache as cache_module
import embykeeper.schedule as schedule_module
from embykeeper.schedule import Scheduler


@pytest.fixture()
def scheduler_env(monkeypatch):
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 11, 5, 12)
    monkeypatch.setattr(schedule_module, "datetime", clock)
    monkeypatch.setattr(schedule_module, "config", SimpleNamespace(debug_cron=False, nofail=True))
    data = {}
    monkeypatch.setattr(
        cache_module,
        "cache",
        SimpleNamespace(get=data.get, set=data.__setitem__, delete=data.pop),
    )
    return clock, data


@pytest.mark.parametrize("hours", [[48, 60], [48.5, 60.5], [48, 48]])
@pytest.mark.parametrize("endpoint", [0, 1])
def test_hourly_next_time(scheduler_env, monkeypatch, hours, endpoint):
    clock, data = scheduler_env
    uniform = Mock(return_value=hours[endpoint])
    monkeypatch.setattr(schedule_module.random, "uniform", uniform)
    scheduler = Scheduler(func=AsyncMock(), interval_hours=hours, sid="hourly")

    expected = clock.now() + timedelta(hours=hours[endpoint])
    assert scheduler.next_time == expected
    assert data["scheduler.hourly"]["next_time"] == expected.isoformat()
    uniform.assert_called_once_with(*hours)


@pytest.mark.parametrize(
    "hours",
    [
        48,
        "<48,60>",
        [],
        [48],
        [48, 60, 72],
        [60, 48],
        [0, 60],
        [-1, 60],
        [True, 60],
        [48, "60"],
        [48, float("nan")],
        [48, float("inf")],
    ],
)
def test_invalid_hourly_interval(scheduler_env, hours):
    with pytest.raises(ValueError, match="interval_hours"):
        Scheduler(func=AsyncMock(), interval_hours=hours)


def test_hourly_restart_reuses_cache(scheduler_env, monkeypatch):
    clock, _ = scheduler_env
    uniform = Mock(return_value=54)
    monkeypatch.setattr(schedule_module.random, "uniform", uniform)
    first = Scheduler(func=AsyncMock(), interval_hours=[48, 60], sid="hourly")
    expected = first.next_time
    clock.now.return_value += timedelta(hours=24)

    restarted = Scheduler(func=AsyncMock(), interval_hours=[48, 60], sid="hourly")
    assert restarted.next_time == expected
    uniform.assert_called_once_with(48, 60)


@pytest.mark.parametrize("expire_offset", [0, 1])
def test_hourly_expired_cache_is_recalculated(scheduler_env, monkeypatch, expire_offset):
    clock, _ = scheduler_env
    uniform = Mock(return_value=54)
    monkeypatch.setattr(schedule_module.random, "uniform", uniform)
    first = Scheduler(func=AsyncMock(), interval_hours=[48, 60], sid="hourly")
    clock.now.return_value = first.next_time + timedelta(seconds=expire_offset)

    restarted = Scheduler(func=AsyncMock(), interval_hours=[48, 60], sid="hourly")
    assert restarted.next_time == clock.now() + timedelta(hours=54)
    assert uniform.call_count == 2


def test_hourly_interval_change_invalidates_cache(scheduler_env, monkeypatch):
    clock, _ = scheduler_env
    monkeypatch.setattr(schedule_module.random, "uniform", Mock(return_value=54))
    first = Scheduler(func=AsyncMock(), interval_hours=[48, 60], sid="hourly")
    assert first.next_time == clock.now() + timedelta(hours=54)
    monkeypatch.setattr(schedule_module.random, "uniform", Mock(return_value=72))

    changed = Scheduler(func=AsyncMock(), interval_hours=[72, 72], sid="hourly")
    assert changed.next_time == clock.now() + timedelta(hours=72)


def test_switching_schedule_mode_invalidates_cache(scheduler_env, monkeypatch):
    clock, _ = scheduler_env
    daily_time = clock.now() + timedelta(days=1)
    daily_random = Mock(return_value=daily_time)
    monkeypatch.setattr(schedule_module, "next_random_datetime", daily_random)
    daily = Scheduler.from_str(func=AsyncMock(), interval_days="1", time_range="12:00", sid="site")
    assert daily.next_time == daily_time

    monkeypatch.setattr(schedule_module.random, "uniform", Mock(return_value=54))
    hourly = Scheduler(func=AsyncMock(), interval_hours=[48, 60], sid="site")
    assert hourly.next_time == clock.now() + timedelta(hours=54)

    restored = Scheduler.from_str(func=AsyncMock(), interval_days="1", time_range="12:00", sid="site")
    assert restored.next_time == daily_time
    assert daily_random.call_count == 2


@pytest.mark.parametrize("days", ["1", "2"])
def test_daily_schedule_and_cache_hash_unchanged(scheduler_env, monkeypatch, days):
    clock, _ = scheduler_env
    expected = clock.now() + timedelta(days=int(days))
    daily_random = Mock(return_value=expected)
    monkeypatch.setattr(schedule_module, "next_random_datetime", daily_random)
    scheduler = Scheduler.from_str(
        func=AsyncMock(), interval_days=days, time_range="<12:00,18:00>", sid="daily"
    )
    old_config = {"days": int(days), "start_time": "12:00:00", "end_time": "18:00:00"}
    old_hash = hashlib.md5(json.dumps(old_config, sort_keys=True).encode()).hexdigest()
    assert scheduler._get_scheduler_config() == old_hash
    assert scheduler.next_time == expected
    daily_random.assert_called_once_with(start_time=time(12), end_time=time(18), interval_days=int(days))

    restarted = Scheduler.from_str(
        func=AsyncMock(), interval_days=days, time_range="<12:00,18:00>", sid="daily"
    )
    assert restarted.next_time == expected
    assert daily_random.call_count == 1


def test_hourly_debug_cron_uses_existing_debug_time(scheduler_env, monkeypatch):
    clock, _ = scheduler_env
    monkeypatch.setattr(schedule_module, "config", SimpleNamespace(debug_cron=True))
    expected = clock.now() + timedelta(seconds=10)
    daily_random = Mock(return_value=expected)
    monkeypatch.setattr(schedule_module, "next_random_datetime", daily_random)
    scheduler = Scheduler(func=AsyncMock(), interval_hours=[48, 60])

    assert scheduler.interval_hours is None
    assert scheduler.next_time == expected
    daily_random.assert_called_once_with(
        start_time=expected.time(), end_time=expected.time(), interval_days=0
    )


def test_hourly_interval_starts_after_task_finishes(scheduler_env, monkeypatch):
    clock, data = scheduler_env
    started = clock.now()
    finished = started + timedelta(hours=54, minutes=7)

    async def run(ctx):
        clock.now.return_value = finished

    func = AsyncMock(side_effect=run)
    callback = Mock(return_value=None)
    monkeypatch.setattr(schedule_module.random, "uniform", Mock(return_value=54))
    monkeypatch.setattr(
        schedule_module.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()])
    )
    scheduler = Scheduler(func=func, interval_hours=[48, 60], sid="hourly", on_next_time=callback)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.schedule())

    func.assert_awaited_once_with(None)
    assert callback.call_args_list[0].args == (started + timedelta(hours=54),)
    assert callback.call_args_list[1].args == (finished + timedelta(hours=54),)
    assert data["scheduler.hourly"]["next_time"] == (finished + timedelta(hours=54)).isoformat()
