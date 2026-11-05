import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from embykeeper.api import create_app
import embykeeper.api.auth as auth_api
import embykeeper.api.config as config_api
import embykeeper.api.runinfo as runinfo_api
from embykeeper.runinfo import RunContext, RunStatus


@pytest.fixture()
def api_env(monkeypatch):
    # create_app imports modules with module-level asyncio primitives. Earlier
    # asyncio.run() tests may have removed the main thread's event loop.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    monkeypatch.setenv("EK_WEBPASS", "test-password")
    data = {}
    cache = Mock()
    cache.get.side_effect = data.get
    cache.set.side_effect = data.__setitem__
    cache.delete.side_effect = lambda key: data.pop(key, None)
    cache.find_by_prefix.side_effect = lambda prefix: [key for key in data if key.startswith(prefix)]
    monkeypatch.setattr(auth_api, "cache", cache)
    monkeypatch.setattr(runinfo_api, "cache", cache)
    read_config = Mock(return_value={"nofail": False})
    write_config = Mock()
    monkeypatch.setattr(config_api, "_read_config_dict", read_config)
    monkeypatch.setattr(config_api, "_write_config_dict", write_config)
    contexts = Mock()
    contexts.get.return_value = None
    monkeypatch.setattr(runinfo_api, "RunContext", contexts)
    monkeypatch.setattr(runinfo_api, "_running_runs", {})

    with TestClient(create_app()) as client:
        yield SimpleNamespace(
            client=client,
            data=data,
            cache=cache,
            contexts=contexts,
            read_config=read_config,
            write_config=write_config,
        )
    loop.close()
    asyncio.set_event_loop(None)


def _login(client, credential):
    response = client.post("/api/auth/login", json={"password": "test-password"})
    assert response.status_code == 200
    token = response.json()["token"]
    assert client.cookies.get(auth_api.SESSION_COOKIE_NAME) == token
    if credential == "bearer":
        client.cookies.clear()
        return {"Authorization": f"Bearer {token}"}
    return {}


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/config"),
        ("POST", "/api/config"),
        ("GET", "/api/settings"),
        ("POST", "/api/settings"),
        ("GET", "/api/settings/basic"),
        ("POST", "/api/settings/basic"),
        ("GET", "/api/settings/notifier"),
        ("POST", "/api/settings/notifier"),
        ("GET", "/api/settings/scheduler"),
        ("POST", "/api/settings/scheduler"),
        ("GET", "/api/settings/network"),
        ("POST", "/api/settings/network"),
        ("GET", "/api/runinfo"),
        ("GET", "/api/runinfo/running"),
        ("GET", "/api/runinfo/logs"),
        ("GET", "/api/runinfo/summary"),
        ("GET", "/api/runinfo/RUN001"),
        ("GET", "/api/runinfo/RUN001/children"),
        ("GET", "/api/runinfo/RUN001/logs"),
        ("GET", "/api/runinfo/RUN001/parents"),
        ("POST", "/api/runinfo/RUN001/cancel"),
        ("POST", "/api/runinfo/cancel_all"),
        ("POST", "/api/runinfo/cache/clear"),
        ("POST", "/api/runinfo/trigger?type=invalid"),
    ],
)
def test_business_endpoints_require_auth(api_env, method, path):
    response = api_env.client.request(method, path, json={} if method == "POST" else None)

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    api_env.read_config.assert_not_called()
    api_env.write_config.assert_not_called()
    api_env.contexts.get.assert_not_called()
    api_env.contexts.cancel_all.assert_not_called()
    api_env.cache.find_by_prefix.assert_not_called()
    api_env.cache.delete_many.assert_not_called()


@pytest.mark.parametrize("credential", ["bearer", "cookie"])
@pytest.mark.parametrize(
    "method, path",
    [("GET", "/api/config"), ("POST", "/api/config"), ("POST", "/api/runinfo/cancel_all")],
)
def test_business_endpoints_reject_invalid_sessions(api_env, credential, method, path):
    headers = {}
    if credential == "bearer":
        headers["Authorization"] = "Bearer invalid-token"
    else:
        api_env.client.cookies.set(auth_api.SESSION_COOKIE_NAME, "invalid-token")

    response = api_env.client.request(method, path, headers=headers, json={} if method == "POST" else None)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid session"}
    api_env.read_config.assert_not_called()
    api_env.write_config.assert_not_called()
    api_env.contexts.cancel_all.assert_not_called()


@pytest.mark.parametrize("credential", ["bearer", "cookie"])
def test_authenticated_config_settings_and_cancel(api_env, credential):
    client = api_env.client
    assert client.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    headers = _login(client, credential)

    assert client.get("/api/auth/me", headers=headers).status_code == 200
    response = client.get("/api/config", headers=headers)
    assert response.status_code == 200
    assert response.json()["nofail"] is False
    response = client.post("/api/config", headers=headers, json={"nofail": True})
    assert response.status_code == 200
    assert response.json() == {"success": True}
    api_env.write_config.assert_called_once_with({"nofail": True})

    for section in ["", "/basic", "/notifier", "/scheduler", "/network"]:
        assert client.get(f"/api/settings{section}", headers=headers).status_code == 200
        response = client.post(f"/api/settings{section}", headers=headers, json={})
        assert response.status_code == 200
        assert response.json() == {"success": True}

    response = client.post("/api/runinfo/cancel_all", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"success": True}
    api_env.contexts.cancel_all.assert_called_once_with()

    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/config", headers=headers).status_code == 401


@pytest.mark.parametrize("webpass", [None, ""])
def test_backend_without_webpass_remains_open_behind_panel_proxy(api_env, monkeypatch, webpass):
    # ProcessManager removes EK_WEBPASS when it launches the backend. The
    # authenticated panel proxy forwards its own bearer/cookie session unchanged.
    if webpass is None:
        monkeypatch.delenv("EK_WEBPASS")
    else:
        monkeypatch.setenv("EK_WEBPASS", webpass)
    headers = {"Authorization": "Bearer panel-session"}
    api_env.client.cookies.set(auth_api.SESSION_COOKIE_NAME, "panel-session")

    assert api_env.client.get("/api/config", headers=headers).status_code == 200
    assert api_env.client.post("/api/config", headers=headers, json={"nofail": True}).status_code == 200
    assert api_env.client.get("/api/settings", headers=headers).status_code == 200
    assert api_env.client.get("/api/runinfo/summary", headers=headers).status_code == 200
    assert api_env.client.post("/api/runinfo/cancel_all", headers=headers).status_code == 200
    api_env.contexts.cancel_all.assert_called_once_with()
    api_env.cache.get.assert_not_called()


@pytest.mark.parametrize("credential", [None, "invalid", "bearer", "cookie"])
def test_log_stream_requires_valid_session(api_env, monkeypatch, credential):
    # Keep the stream finite even if the auth regression returns: TestClient
    # buffers StreamingResponse, so a real endless SSE generator would hang.
    stream_response = Mock(
        side_effect=lambda content, **kwargs: StreamingResponse(
            iter(['data: {"message": "test-log"}\n\n']), **kwargs
        )
    )
    monkeypatch.setattr(runinfo_api, "StreamingResponse", stream_response)
    headers = {"Accept": "text/event-stream"}
    if credential in {"bearer", "cookie"}:
        headers.update(_login(api_env.client, credential))
    elif credential == "invalid":
        headers["Authorization"] = "Bearer invalid-token"

    response = api_env.client.get("/api/runinfo/logs/stream", headers=headers)

    if credential in {"bearer", "cookie"}:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text == 'data: {"message": "test-log"}\n\n'
        stream_response.assert_called_once()
    else:
        assert response.status_code == 401
        stream_response.assert_not_called()
    api_env.cache.find_by_prefix.assert_not_called()


def test_summary_route_precedes_run_id_and_keeps_detail_paths(api_env):
    headers = _login(api_env.client, "bearer")
    runs = [
        RunContext(
            id="RUN001", status=RunStatus.RUNNING, start_time=datetime(2026, 1, 1, tzinfo=timezone.utc)
        ),
        RunContext(
            id="RUN002", status=RunStatus.SUCCESS, start_time=datetime(2026, 1, 2, tzinfo=timezone.utc)
        ),
        RunContext(id="RUN003", status=RunStatus.FAIL),
        RunContext(id="RUN004", status=RunStatus.CANCELLED),
        RunContext(id="CHILD1", parent_ids=["RUN001"], status=RunStatus.SUCCESS),
    ]
    by_id = {run.id: run for run in runs}
    api_env.data.update({f"runinfo.{run.id}": "cached" for run in runs})
    api_env.contexts.get.side_effect = by_id.get

    response = api_env.client.get("/api/runinfo/summary", headers=headers)

    assert response.status_code == 200
    summary = response.json()
    assert {key: summary[key] for key in ["total", "running", "success", "failed", "cancelled"]} == {
        "total": 4,
        "running": 1,
        "success": 1,
        "failed": 1,
        "cancelled": 1,
    }
    assert [run["id"] for run in summary["recent"][:2]] == ["RUN002", "RUN001"]
    assert {run["id"] for run in summary["recent"]} == {"RUN001", "RUN002", "RUN003", "RUN004"}
    assert all(call.args != ("summary",) for call in api_env.contexts.get.call_args_list)

    response = api_env.client.get("/api/runinfo/RUN001", headers=headers)
    assert response.status_code == 200
    assert response.json()["run"]["id"] == "RUN001"
    response = api_env.client.get("/api/runinfo/UNKNOWN", headers=headers)
    assert response.status_code == 404
    assert response.json() == {"detail": "Run not found"}
