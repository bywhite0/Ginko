"""Real TCP/WebSocket tests run in isolated processes because NoneBot has global registries."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from ginko.config import RuntimeConfig, RuntimeSettings
from ginko.gateway import Gateway
from ginko.instance import InstanceLock, InstanceRunningError
from ginko.persona import load_ginko
from ginko.runtime import Runtime
from ginko.storage.database import Database


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def packet(message_id, **changes):
    return {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": 10000,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": 20001,
        "message": [{"type": "text", "data": {"text": "synthetic gateway probe"}}],
        "raw_message": "synthetic gateway probe",
        "font": 0,
        "sender": {"user_id": 20001, "nickname": "Synthetic test user"},
    } | changes


async def exercise(directory, mode):
    raw = tomllib.loads((Path(__file__).parents[1] / "config.example.toml").read_text())
    config = RuntimeConfig(
        RuntimeSettings.model_validate(raw),
        directory / "data",
        load_ginko(),
        SecretStr("synthetic-token"),
        SecretStr("synthetic-key"),
    )
    # This is intentionally invalid and must never be read by the service configuration.
    os.chdir(directory)
    Path(".env").write_text("HOST=0.0.0.0\nPORT=invalid\nCOMMAND_START=invalid-json\n")
    os.environ.update(
        HOST="0.0.0.0",
        COMMAND_START="invalid-json",
        ONEBOT_V11_ACCESS_TOKEN="foreign-token",
        ONEBOT_V11_WS_URLS="invalid-json",
        FASTAPI_RELOAD="true",
    )
    decision_entered, release_decision = asyncio.Event(), asyncio.Event()
    decisions = []

    async def decide(claim):
        decisions.append(claim.event.event_id)
        decision_entered.set()
        if mode == "worker-failure":
            raise ValueError("synthetic sensitive exception")
        await release_decision.wait()
        return "synthetic reply"

    async def send(delivery):
        # Synthetic decision, real SDK API exchange and receipt correlation over TCP.
        bot = gateway.adapter.bots["10000"]
        response = await bot.send_private_msg(
            user_id=int(delivery.session.chat_id),
            message=[{"type": "text", "data": {"text": delivery.text}}],
        )
        return str(response["message_id"])

    runtime = Runtime(config, decide, send)
    gateway = Gateway(runtime)
    assert str(gateway.driver.config.host) == "127.0.0.1"
    assert not gateway.driver.fastapi_config.fastapi_reload
    assert gateway.adapter.onebot_config.onebot_ws_urls == set()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    serving = asyncio.create_task(gateway.serve(sockets=[listener]))
    headers = {
        "Authorization": "Bearer synthetic-token",
        "X-Self-ID": "10000",
        "X-Client-Role": "Universal",
    }
    url = f"ws://127.0.0.1:{port}/onebot/v11/ws"

    def count():
        return runtime.database.connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]

    def status(table):
        row = runtime.database.connection.execute(f"SELECT status FROM {table} LIMIT 1").fetchone()
        return row[0] if row else None

    try:
        await until(lambda: gateway.server.started or serving.done())
        assert not serving.done()
        with pytest.raises(InstanceRunningError), InstanceLock(config.data_dir):
            pytest.fail("service failed to acquire its lock")
        if mode == "normal":
            for invalid in (
                headers | {"Authorization": "Bearer wrong-token"},
                headers | {"X-Self-ID": "10001"},
                headers | {"X-Client-Role": "Event"},
                {key: value for key, value in headers.items() if key != "Authorization"},
                {key: value for key, value in headers.items() if key != "X-Client-Role"},
            ):
                with pytest.raises(InvalidStatus) as failure:
                    async with connect(url, additional_headers=invalid, proxy=None):
                        pytest.fail("invalid handshake accepted")
                assert failure.value.response.status_code == 403
            assert not gateway.adapter.bots

            def http_ingress():
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/onebot/v11/http",
                    data=json.dumps(packet(99)).encode(),
                    headers={"X-Self-ID": "10000", "Content-Type": "application/json"},
                )
                with pytest.raises(urllib.error.HTTPError) as failure:
                    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                        request, timeout=3
                    )
                assert failure.value.code == 405

            await asyncio.to_thread(http_ingress)
            assert count() == 0

        async with connect(url, additional_headers=headers, proxy=None) as peer:
            await until(lambda: runtime._connected)
            if mode == "normal":
                with pytest.raises(InvalidStatus):
                    async with connect(url, additional_headers=headers, proxy=None):
                        pytest.fail("duplicate connection accepted")
                for rejected in (
                    packet(80, self_id=10001),
                    packet(81, user_id=20002),
                    packet(82, message="[CQ:reply,id=1]unsupported quote"),
                ):
                    await peer.send(json.dumps(rejected))
                await asyncio.sleep(0.05)
                assert count() == 0
            if mode == "ingress-failure":
                runtime.database.connection.execute(
                    """CREATE TRIGGER fail_ingress BEFORE INSERT ON inbox
                    BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END"""
                )
                await peer.send(json.dumps(packet(1)))
                with pytest.raises(RuntimeError, match="runtime task failed: IntegrityError"):
                    await serving
                assert not runtime.ingress.accepting
                assert not decisions
                return
            await peer.send(json.dumps(packet(1)))
            await decision_entered.wait()
            if mode == "worker-failure":
                with pytest.raises(RuntimeError, match="runtime task failed: ValueError"):
                    await serving
                assert not runtime.ingress.accepting
                return
            await peer.send(json.dumps(packet(1)))  # Redelivery while the model awaits.
            await peer.send(json.dumps(packet(2)))
            await until(lambda: count() == 2)
            assert len(decisions) == 1
            if mode == "cancel":
                serving.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await serving
                assert not runtime.ingress.accepting
                return
            release_decision.set()
            action = json.loads(await asyncio.wait_for(peer.recv(), timeout=5))
            assert action["action"] == "send_private_msg"
            assert action["params"]["user_id"] == 20001
            assert action["params"]["message"][0]["data"]["text"] == "synthetic reply"
            if mode == "lost-receipt":
                gateway.server.should_exit = True
                await serving
                return
            await peer.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": 501},
                        "echo": action["echo"],
                    }
                )
            )
            action = json.loads(await asyncio.wait_for(peer.recv(), timeout=5))
            await peer.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": 502},
                        "echo": action["echo"],
                    }
                )
            )
            await until(lambda: status("outbox") == "sent")
        await until(lambda: not runtime._connected)
        # The old connection must release the slot so a reconnect can ingest new work.
        async with connect(url, additional_headers=headers, proxy=None) as peer:
            await until(lambda: runtime._connected)
            await peer.send(json.dumps(packet(1)))
            await asyncio.sleep(0.05)
            assert count() == 2
            assert len(decisions) == 2
    finally:
        gateway.server.should_exit = True
        if not serving.done():
            await asyncio.wait_for(serving, timeout=10)
        listener.close()
        assert not gateway.adapter.tasks
        assert not gateway.adapter.connections
        assert not gateway.driver.bots
        assert gateway.matcher is None
        assert runtime.database is None
        assert all(task.done() for task in runtime._tasks)
        with InstanceLock(config.data_dir):
            pass


@pytest.mark.parametrize(
    "mode", ["normal", "cancel", "worker-failure", "lost-receipt", "ingress-failure"]
)
def test_real_websocket_lifecycle_in_isolated_process(tmp_path, mode):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path), mode],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with Database(tmp_path / "data/ginko.sqlite3") as database:
        rows = database.connection.execute("SELECT status FROM outbox ORDER BY seq").fetchall()
        if mode == "normal":
            assert [row[0] for row in rows] == ["sent", "sent"]
        elif mode == "lost-receipt":
            assert rows[0][0] == "unknown"
        else:
            assert rows == []


if __name__ == "__main__":
    asyncio.run(exercise(Path(sys.argv[1]), sys.argv[2]))
