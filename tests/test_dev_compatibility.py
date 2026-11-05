import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
import pytest
import tomli

from embykeeper.api import sites
from embykeeper.config import ConfigManager
from embykeeper.schema import Config, ProxyConfig, TelegramAccount, TelegramConfig
import embykeeper.telegram as telegram_module
from embykeeper.telegram.checkiner import _webapp, nebula


@pytest.mark.parametrize("use_proxy", [False, True])
def test_webapp_session_honours_telegram_proxy(monkeypatch, use_proxy):
    proxy = ProxyConfig(hostname="127.0.0.1", port=7890, scheme="http")
    monkeypatch.setattr(
        telegram_module, "config", Config(proxy=proxy, telegram=TelegramConfig(use_proxy=use_proxy))
    )
    session = Mock()
    monkeypatch.setattr(_webapp, "AsyncSession", session)
    _webapp.webapp_session()
    assert session.call_args.kwargs["proxy"] == ("http://127.0.0.1:7890" if use_proxy else None)
    assert session.call_args.kwargs["impersonate"] == "safari184_ios"
    assert session.call_args.kwargs["headers"]["User-Agent"] == _webapp.IOS_WEBAPP_USER_AGENT


def test_webapp_session_preserves_explicit_proxy_override(monkeypatch):
    monkeypatch.setattr(_webapp, "get_telegram_proxy_str", Mock(return_value=None))
    session = Mock()
    monkeypatch.setattr(_webapp, "AsyncSession", session)
    _webapp.webapp_session(proxy="http://127.0.0.1:8080")
    assert session.call_args.kwargs["proxy"] == "http://127.0.0.1:8080"


def test_nebula_loads_webapp_before_api_requests(monkeypatch):
    auth = _webapp.WebAppAuth(
        page_url="https://example.test/app",
        url="https://example.test/app#data",
        base_url="https://example.test",
        data="test-init-data",
    )
    monkeypatch.setattr(nebula, "get_webview_auth", AsyncMock(return_value=auth))
    calls = []

    async def load_page(session, url):
        calls.append(("page", url))

    async def get_info(url, **kwargs):
        calls.append(("info", url))
        return SimpleNamespace(
            json=lambda: {
                "message": "Success",
                "data": {
                    "balance": 10,
                    "next_check_in": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                },
            }
        )

    async def post_checkin(url, **kwargs):
        calls.append(("checkin", url))
        return SimpleNamespace(json=lambda: {"message": "Success", "data": {"coin": 1}})

    session = SimpleNamespace(get=AsyncMock(side_effect=get_info), post=AsyncMock(side_effect=post_checkin))
    context = AsyncMock()
    context.__aenter__.return_value = session
    monkeypatch.setattr(nebula, "webapp_session", Mock(return_value=context))
    monkeypatch.setattr(nebula, "load_webapp_page", AsyncMock(side_effect=load_page))
    checkiner = nebula.NebulaCheckin.__new__(nebula.NebulaCheckin)
    checkiner.client = Mock()
    checkiner.log = Mock()
    checkiner.finish = AsyncMock()
    asyncio.run(checkiner.send_checkin())
    assert calls == [
        ("page", auth.page_url),
        ("info", f"{auth.base_url}/api/v1/tg/info"),
        ("checkin", f"{auth.base_url}/api/v1/tg/checkin"),
    ]
    assert session.get.call_args.kwargs["headers"]["X-Initdata"] == auth.data
    checkiner.finish.assert_awaited_once()


@pytest.mark.parametrize("template", [None, "b"])
def test_panel_site_options_accept_hourly_ranges(template):
    options = sites._validate_options("checkiner", template, {"interval_hours": [48.5, 60.5]})
    assert options["interval_hours"] == [48.5, 60.5]
    fields = {option["key"]: option for option in sites.UNIVERSAL_OPTIONS["checkiner"]}
    assert fields["interval_hours"]["type"] == "float|list"


@pytest.mark.parametrize("template", [None, "b"])
@pytest.mark.parametrize(
    "options",
    [
        {"interval_hours": [48, 60], "interval_days": 2},
        {"interval_hours": [48, 60], "time_range": "12:00"},
        {"interval_hours": [60, 48]},
        {"interval_hours": [48]},
        {"interval_hours": [False, 60]},
        {"interval_hours": "<48,60>"},
    ],
)
def test_panel_site_options_reject_invalid_hourly_settings(template, options):
    with pytest.raises(HTTPException) as exc:
        sites._validate_options("checkiner", template, options)
    assert exc.value.status_code == 400
    assert "interval_hours" in exc.value.detail


def test_dev_account_settings_survive_config_round_trip():
    account = TelegramAccount(
        phone="test-account",
        session="test-session",
        skip_remote_auth=True,
        ai_base_url="https://example.test/v1",
        ai_model="test-model",
        ai_api_key="test-key",
    )
    config = Config(telegram=TelegramConfig(account=[account]))
    restored = Config(**config.model_dump(mode="json", exclude_none=True, exclude_unset=True))
    assert restored.telegram.account[0] == account


def test_example_config_preserves_optional_dev_fields():
    config = tomli.loads(ConfigManager.generate_example_config())
    assert config["telegram"]["account"][0]["skip_remote_auth"] is False
    for key in ("ai_base_url", "ai_model", "ai_api_key"):
        assert config["checkiner"][key] == ""


def test_legacy_docker_layout_preserves_dev_python_version():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "Dockerfile").exists()
    dockerfile = (root / "deploy" / "legacy" / "Dockerfile").read_text()
    assert "FROM python:3.10.16 AS builder" in dockerfile
    assert "FROM python:3.10.16-slim" in dockerfile
    assert "/src/deploy/legacy/entrypoint.sh" in dockerfile
    assert (root / "deploy" / "legacy" / "entrypoint.sh").exists()
    dev_dockerfile = (root / "deploy" / "legacy" / "Dockerfile.dev").read_text()
    assert "FROM python:3.10.16" in dev_dockerfile
    assert "/build/deploy/legacy/entrypoint-dev.sh" in dev_dockerfile
