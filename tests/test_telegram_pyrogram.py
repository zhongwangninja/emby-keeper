import asyncio
import sqlite3
from unittest.mock import AsyncMock, Mock

import pytest
from pyrogram.session import Session
from pyrogram.session.session import SessionState

from embykeeper.telegram.pyrogram import Client


async def make_client():
    client = Client("test", api_id=12345, api_hash="test", in_memory=True, no_updates=True)
    await client.storage.open()
    await client.storage.api_id(12345)
    client.dispatcher.stop = AsyncMock()
    client.is_connected = True
    client.is_initialized = True
    return client


async def make_session(client, dc_id, is_media=False):
    session = Session(client, dc_id, "127.0.0.1", 443, b"\0" * 256, False, is_media=is_media)
    closed = asyncio.Event()

    async def recv():
        await closed.wait()
        return None

    session.connection = Mock()
    session.connection.recv = AsyncMock(side_effect=recv)
    session.connection.close = AsyncMock(side_effect=closed.set)
    session.send = AsyncMock()
    await session._set_state(SessionState.STARTED)
    session.is_started.set()
    session.ping_task = asyncio.create_task(session.ping_worker())
    session.recv_task = asyncio.create_task(session.recv_worker())
    return session


@pytest.mark.parametrize("auxiliary_count", [0, 1, 2])
def test_client_stop_closes_auxiliary_sessions(auxiliary_count):
    async def run():
        client = await make_client()
        client.session = await make_session(client, 2)
        client.media_sessions[3] = await make_session(client, 3, is_media=True)
        for dc_id in range(3, 3 + auxiliary_count):
            client.sessions[dc_id] = await make_session(client, dc_id)
        sessions = [client.session, *client.media_sessions.values(), *client.sessions.values()]
        workers = [(session.ping_task, session.recv_task) for session in sessions]
        storage_close = client.storage.close

        async def close():
            # Every connection must be stopped before its shared database is closed.
            assert all(session.state == SessionState.STOPPED for session in sessions)
            await storage_close()

        client.storage.close = AsyncMock(side_effect=close)
        try:
            await client.stop()

            assert client.session is None
            assert client.sessions == {}
            assert client.media_sessions == {}
            assert client.is_connected is False
            assert client.is_initialized is False
            for session, (ping_task, recv_task) in zip(sessions, workers):
                session.connection.close.assert_awaited_once()
                assert not session.is_started.is_set()
                assert ping_task.done()
                assert recv_task.done()
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                await client.storage.api_id()
        finally:
            for session in sessions:
                await session.stop()
            await storage_close()

    asyncio.run(run())


def test_client_terminate_preserves_handlers_option():
    async def run():
        client = await make_client()
        session = await make_session(client, 3)
        client.sessions[3] = session
        try:
            await client.terminate(clear_handlers=False)

            client.dispatcher.stop.assert_awaited_once_with(clear_handlers=False)
            assert client.sessions == {}
            assert session.state == SessionState.STOPPED
            assert await client.storage.api_id() == 12345
        finally:
            await session.stop()
            await client.storage.close()

    asyncio.run(run())


def test_client_terminate_rejects_uninitialized_client():
    async def run():
        client = await make_client()
        client.is_initialized = False
        session = Mock(stop=AsyncMock())
        client.sessions[3] = session
        try:
            with pytest.raises(ConnectionError, match="already terminated"):
                await client.terminate()

            session.stop.assert_not_awaited()
            assert client.sessions == {3: session}
        finally:
            await client.storage.close()

    asyncio.run(run())
