import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import embykeeper.telegram.session as session_module
from embykeeper.telegram.session import ClientsSession


@pytest.fixture()
def time_check_env(monkeypatch):
    session = ClientsSession.__new__(ClientsSession)
    monkeypatch.setattr(ClientsSession, "proxy", property(lambda self: None))
    client = AsyncMock()
    client.__aenter__.return_value = client
    monkeypatch.setattr(session_module.httpx, "AsyncClient", Mock(return_value=client))
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(session_module, "datetime", clock)
    log = Mock()
    monkeypatch.setattr(session_module, "logger", log)
    show_exception = Mock()
    monkeypatch.setattr(session_module, "show_exception", show_exception)
    return session, client, clock, log, show_exception


@pytest.mark.parametrize("content", [b"135.148.55.92", b"<html>error</html>", b"", b"\xff"])
def test_invalid_time_response_is_skipped(time_check_env, content):
    session, client, clock, log, show_exception = time_check_env
    client.get.return_value = SimpleNamespace(status_code=200, content=content)

    assert asyncio.run(session.test_time()) is False

    log.warning.assert_called_once()
    assert "无效的时间戳" in log.warning.call_args.args[0]
    clock.now.assert_not_called()
    show_exception.assert_not_called()


@pytest.mark.parametrize("offset", [0, 30, -30, 31, -31])
@pytest.mark.parametrize("whitespace", [False, True])
def test_valid_time_response_preserves_clock_check(time_check_env, offset, whitespace):
    session, client, clock, log, show_exception = time_check_env
    timestamp = int((clock.now.return_value.timestamp() + offset) * 1000)
    content = str(timestamp).encode()
    if whitespace:
        content = b" " + content + b"\n"
    client.get.return_value = SimpleNamespace(status_code=200, content=content)

    asyncio.run(session.test_time())

    clock.now.assert_called_once_with(timezone.utc)
    if abs(offset) > 30:
        log.warning.assert_called_once()
        assert "系统时间设置不正确" in log.warning.call_args.args[0]
    else:
        log.warning.assert_not_called()
    show_exception.assert_not_called()


@pytest.mark.parametrize("status_code", [403, 500])
def test_failed_time_response_is_skipped(time_check_env, status_code):
    session, client, clock, log, show_exception = time_check_env
    client.get.return_value = SimpleNamespace(status_code=status_code, content=b"error")

    assert asyncio.run(session.test_time()) is False

    log.warning.assert_called_once()
    assert "世界时间接口异常" in log.warning.call_args.args[0]
    clock.now.assert_not_called()
    show_exception.assert_not_called()


def test_time_network_error_is_skipped(time_check_env):
    session, client, clock, log, show_exception = time_check_env
    client.get.side_effect = httpx.ConnectError("connection failed")

    assert asyncio.run(session.test_time()) is False

    log.warning.assert_called_once()
    clock.now.assert_not_called()
    show_exception.assert_not_called()
