import asyncio
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import tomli

import embykeeper.cache as cache_module
import embykeeper.schedule as schedule_module
import embykeeper.telegram.checkin_main as checkin_module
from embykeeper.runinfo import RunStatus
from embykeeper.schema import CheckinerConfig, Config, SiteConfig, TelegramAccount
from embykeeper.telegram.checkin_main import CheckinerManager

SITE = "templ_b<GetfreeCloud_Bot>"


@pytest.fixture()
def checkin_env(monkeypatch):
    checkiner_config = CheckinerConfig(**tomli.loads("""
[checkiner]
time_range = "<12:00,18:00>"
interval_days = 1
random_start = 1

[checkiner."templ_b<GetfreeCloud_Bot>"]
name = "白给宗师bot"
bot_checkin_cmd = ["/upgrade"]
bot_success_keywords = "白给"
interval_hours = [48, 60]
""")["checkiner"])
    account = TelegramAccount(phone="test-account", site=SiteConfig(checkiner=[SITE, "normal"]))
    monkeypatch.setattr(checkin_module, "config", Config(checkiner=checkiner_config))
    monkeypatch.setattr(schedule_module, "config", SimpleNamespace(debug_cron=False, nofail=True))
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 11, 5, 12)
    monkeypatch.setattr(schedule_module, "datetime", clock)
    data = {}
    monkeypatch.setattr(
        cache_module, "cache", SimpleNamespace(get=data.get, set=data.__setitem__, delete=data.pop)
    )
    context = SimpleNamespace(id="context")
    contexts = Mock()
    contexts.get_or_create.return_value = context
    contexts.prepare.return_value = context
    monkeypatch.setattr(checkin_module, "RunContext", contexts)
    log = Mock()
    log.bind.return_value = log
    monkeypatch.setattr(checkin_module, "logger", log)
    hourly = type("HourlyCheckin", (), {"templ_name": SITE})
    normal = type("NormalCheckin", (), {"templ_name": "normal"})
    monkeypatch.setattr(checkin_module, "get_cls", Mock(return_value=[hourly, normal]))
    monkeypatch.setattr(checkin_module, "extract", lambda classes: classes)

    manager = CheckinerManager.__new__(CheckinerManager)
    manager._accounts = {}
    manager._schedulers = {}
    manager._scheduler_tasks = {}
    manager._scheduler_signatures = {}
    return manager, account, checkiner_config, clock, data, log


def build_site_scheduler(manager, account, settings):
    account.checkiner_config = settings
    return manager._build_scheduler_specs(account)[f"{account.phone}.{SITE}"].factory()


@pytest.mark.parametrize(
    "site_config, expected",
    [
        ({}, False),
        ({"interval_days": "2"}, False),
        ({"time_range": "12:00"}, True),
        ({"interval_hours": [48, 60]}, True),
    ],
)
def test_independent_schedule_detection(checkin_env, site_config, expected):
    manager, _, _, _, _, _ = checkin_env
    config = CheckinerConfig(**{SITE: site_config})
    assert manager._has_independent_schedule(SITE, config) is expected


def test_hourly_site_ignores_global_daily_schedule(checkin_env):
    manager, account, config, _, _, _ = checkin_env
    scheduler = build_site_scheduler(manager, account, config)
    assert scheduler.interval_hours == [48, 60]
    assert scheduler.start_time is None
    assert scheduler.end_time is None
    assert scheduler.sid == f"checkiner.{account.phone}.{SITE}"


@pytest.mark.parametrize("field, value", [("time_range", "12:00"), ("interval_days", "2")])
def test_hourly_site_rejects_conflicting_settings(checkin_env, field, value):
    manager, account, _, _, _, _ = checkin_env
    config = CheckinerConfig(**{SITE: {"interval_hours": [48, 60], field: value}})
    with pytest.raises(ValueError, match="不能与"):
        build_site_scheduler(manager, account, config)
    assert not manager._scheduler_tasks


@pytest.mark.parametrize("hours", [None, [60, 48], [48], [0, 60], "<48,60>"])
def test_hourly_site_rejects_invalid_settings(checkin_env, hours):
    manager, account, _, _, _, _ = checkin_env
    config = CheckinerConfig(**{SITE: {"interval_hours": hours}})
    with pytest.raises(ValueError, match="interval_hours"):
        build_site_scheduler(manager, account, config)
    assert not manager._scheduler_tasks


@pytest.mark.parametrize("days", [2, "2"])
def test_existing_independent_time_range_schedule(checkin_env, days):
    manager, account, _, _, _, _ = checkin_env
    config = CheckinerConfig(**{SITE: {"time_range": "<12:00,18:00>", "interval_days": days}})
    scheduler = build_site_scheduler(manager, account, config)
    assert scheduler.interval_hours is None
    assert scheduler.days == 2
    assert scheduler.start_time == time(12)
    assert scheduler.end_time == time(18)


def test_hourly_site_discovery_preserves_account_schedule(checkin_env):
    manager, account, _, _, _, _ = checkin_env
    specs = manager._build_scheduler_specs(account)
    assert set(specs) == {f"{account.phone}.{SITE}", account.phone}
    hourly = specs[f"{account.phone}.{SITE}"].factory()
    daily = specs[account.phone].factory()
    assert hourly.interval_hours == [48, 60]
    assert daily.interval_hours is None
    assert daily.days == 1
    assert daily.start_time == time(12)
    assert daily.end_time == time(18)


def test_hourly_only_account_has_no_daily_batch(checkin_env, monkeypatch):
    manager, account, _, _, _, _ = checkin_env
    hourly = type("HourlyCheckin", (), {"templ_name": SITE})
    monkeypatch.setattr(checkin_module, "get_cls", Mock(return_value=[hourly]))
    specs = manager._build_scheduler_specs(account)
    assert set(specs) == {f"{account.phone}.{SITE}"}
    assert manager._build_batch_specs(account)[0] == []


@pytest.mark.parametrize("instant", [False, True])
def test_hourly_site_is_excluded_from_account_runs(checkin_env, monkeypatch, instant):
    manager, account, _, _, _, _ = checkin_env

    class HourlyCheckin:
        templ_name = SITE

        def __init__(self, *args, **kwargs):
            pytest.fail("Hourly site must not run as part of the account batch")

    class NormalCheckin:
        templ_name = "normal"
        name = "normal"

        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(checkin_module, "get_cls", Mock(return_value=[HourlyCheckin, NormalCheckin]))
    monkeypatch.setattr(
        checkin_module, "Link", Mock(return_value=SimpleNamespace(auth=AsyncMock(return_value=True)))
    )
    monkeypatch.setattr(checkin_module.random, "uniform", Mock(return_value=0.5))
    manager._run_batch_site = AsyncMock(
        side_effect=lambda c, sem, wait: (c, SimpleNamespace(status=RunStatus.SUCCESS))
    )
    client = SimpleNamespace(me=SimpleNamespace(full_name="Test"))
    state = manager._ensure_state(account)
    state.instant = instant
    context = SimpleNamespace(id="run", end_time=None, finish=Mock())

    asyncio.run(manager._run_account(context, state, client))
    manager._run_batch_site.assert_awaited_once()
    assert isinstance(manager._run_batch_site.call_args.args[0], NormalCheckin)
    assert manager._run_batch_site.call_args.args[2] == (0 if instant else 0.5)
    context.finish.assert_called_once()


def test_hourly_restart_logs_cached_next_time(checkin_env, monkeypatch):
    manager, account, config, clock, _, log = checkin_env
    uniform = Mock(return_value=54)
    monkeypatch.setattr(schedule_module.random, "uniform", uniform)
    first = build_site_scheduler(manager, account, config)
    expected = first.next_time
    clock.now.return_value += timedelta(hours=24)

    restarted = build_site_scheduler(manager, account, config)
    monkeypatch.setattr(schedule_module.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(restarted.schedule())

    assert restarted.next_time == expected
    uniform.assert_called_once_with(48, 60)
    message = log.info.call_args.args[0]
    assert "白给宗师bot" in message
    assert expected.strftime("%m-%d %H:%M %p") in message
    assert "48～60 小时" in message


@pytest.mark.parametrize("change", ["hours", "daily", "mode"])
def test_schedule_reload_restarts_only_affected_tasks(checkin_env, monkeypatch, change):
    manager, account, settings, _, _, _ = checkin_env

    async def wait_forever(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(schedule_module.Scheduler, "schedule", wait_forever)

    async def run():
        await manager._reconcile_schedulers(manager._build_scheduler_specs(account))
        await asyncio.sleep(0)
        hourly_key = f"{account.phone}.{SITE}"
        hourly_task = manager._scheduler_tasks[hourly_key]
        daily_task = manager._scheduler_tasks[account.phone]
        hourly_scheduler = manager._schedulers[hourly_key]
        if change == "hours":
            settings.get_site_config(SITE)["interval_hours"] = [72, 84]
        elif change == "daily":
            settings.interval_days = "2"
        else:
            account.checkiner_config = CheckinerConfig(
                **{SITE: {"time_range": "12:00", "interval_days": "2"}}
            )
        try:
            await manager._reconcile_schedulers(manager._build_scheduler_specs(account))
            if change == "daily":
                assert manager._scheduler_tasks[hourly_key] is hourly_task
                assert manager._schedulers[hourly_key] is hourly_scheduler
                assert daily_task.cancelled()
                assert manager._schedulers[account.phone].days == 2
            else:
                assert hourly_task.cancelled()
                assert manager._scheduler_tasks[hourly_key] is not hourly_task
                if change == "hours":
                    assert manager._scheduler_tasks[account.phone] is daily_task
                    assert manager._schedulers[hourly_key].interval_hours == [72, 84]
                else:
                    assert manager._schedulers[hourly_key].interval_hours is None
                    assert manager._schedulers[hourly_key].days == 2
        finally:
            await manager._cancel_scheduler_keys(list(manager._scheduler_tasks))

    asyncio.run(run())
