"""Synthetic fault drills over real HTTP and OneBot WebSocket transports."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr
from websockets.asyncio.client import connect

from ginko.application import Application
from ginko.config import RuntimeConfig, RuntimeSettings
from ginko.instance import InstanceLock
from ginko.persona import load_ginko
from ginko.storage.budget import BudgetLedger, BudgetLimits
from ginko.storage.database import Database


async def until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.01)


def packet(message_id):
    return {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": 10000,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": 20001,
        "message": [{"type": "text", "data": {"text": f"synthetic drill {message_id}"}}],
        "raw_message": f"synthetic drill {message_id}",
        "font": 0,
        "sender": {"user_id": 20001, "nickname": "Synthetic test peer"},
    }


async def exercise(directory: Path, mode: str):
    requests = []
    model_entered, release_model = asyncio.Event(), asyncio.Event()
    handlers = set()

    async def http_model(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            async with asyncio.timeout(10):
                headers = await reader.readuntil(b"\r\n\r\n")
                assert headers.startswith(b"POST /v1/chat/completions HTTP/1.1\r\n")
                length = next(
                    int(line.partition(b":")[2])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                requests.append(json.loads(await reader.readexactly(length)))
                # The real HTTP request must follow a committed, queryable reservation.
                with Database(directory / "data/ginko.sqlite3") as database:
                    attempts = BudgetLedger(database, BudgetLimits(0, 0)).list_model_attempts()
                    assert attempts[-1].budget_status == "reserved"
                model_entered.set()
                await release_model.wait()
                body = json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(
                                        {"action": "reply", "text": "Synthetic reply."}
                                    ),
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 20,
                            "total_tokens": 120,
                        },
                    }
                ).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + body
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    async with await asyncio.start_server(http_model, "127.0.0.1", 0) as model_server:
        port = model_server.sockets[0].getsockname()[1]
        raw = tomllib.loads((Path(__file__).parents[1] / "config.example.toml").read_text())
        raw["model"].update(
            base_url=f"http://127.0.0.1:{port}/v1", timeout_seconds=1.0, rate_windows=[]
        )
        raw["activity"].update(lease_seconds=6, ttl_seconds=60)
        raw["budget"].update(daily_microusd=100_000, monthly_microusd=100_000)
        raw["onebot"]["api_timeout_seconds"] = 2.0
        if mode in {"daily-budget", "monthly-budget"}:
            raw["budget"][mode.replace("-budget", "_microusd")] = 0
        config = RuntimeConfig(
            RuntimeSettings.model_validate(raw),
            directory / "data",
            load_ginko(),
            SecretStr("synthetic-peer-token"),
            SecretStr("synthetic-model-key"),
        )
        app = Application(config)
        runtime, gateway = app.runtime, app.gateway
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        serving = asyncio.create_task(gateway.serve(sockets=[listener]))

        def statuses(table):
            return [
                row[0]
                for row in runtime.database.connection.execute(
                    f"SELECT status FROM {table} ORDER BY seq"
                )
            ]

        async def receive(peer):
            action = json.loads(await asyncio.wait_for(peer.recv(), timeout=10))
            assert action["action"] == "send_private_msg"
            assert action["params"]["user_id"] == 20001
            assert action["params"]["message"][0]["data"]["text"] == "Synthetic reply."
            return action

        async def acknowledge(peer, action, receipt):
            await peer.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": receipt},
                        "echo": action["echo"],
                    }
                )
            )

        try:
            await until(lambda: gateway.server.started or serving.done())
            assert not serving.done()
            if mode == "audit-failure":
                runtime.database.connection.execute("""CREATE TRIGGER reject_outcome
                    BEFORE UPDATE OF outcome_code ON model_attempts
                    BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END""")
            async with connect(
                f"ws://127.0.0.1:{port}/onebot/v11/ws",
                proxy=None,
                additional_headers={
                    "Authorization": "Bearer synthetic-peer-token",
                    "X-Self-ID": "10000",
                    "X-Client-Role": "Universal",
                },
            ) as peer:
                await until(lambda: runtime._connected)
                if mode not in {"roundtrip", "crash"}:
                    release_model.set()
                await peer.send(json.dumps(packet(1)))
                if mode in {"daily-budget", "monthly-budget"}:
                    await until(lambda: statuses("inbox") == ["failed"])
                    assert not requests
                    assert statuses("outbox") == []
                    assert (
                        runtime.database.connection.execute(
                            "SELECT COUNT(*) FROM budget"
                        ).fetchone()[0]
                        == 0
                    )
                    return
                if mode == "audit-failure":
                    with pytest.raises(RuntimeError, match="^runtime task failed: IntegrityError$"):
                        await asyncio.wait_for(serving, timeout=10)
                    assert not runtime.ingress.accepting
                    assert len(requests) == 1
                    return
                if mode == "resume-lost-receipt":
                    assert statuses("outbox") == ["unknown"]
                    await peer.send(json.dumps(packet(2)))
                    await acknowledge(peer, await receive(peer), 502)
                    await until(lambda: statuses("outbox") == ["unknown", "sent"])
                    assert len(requests) == 1
                    return
                if mode == "resume-crash":
                    await peer.send(json.dumps(packet(2)))
                    for receipt in (501, 502):
                        await acknowledge(peer, await receive(peer), receipt)
                    await until(lambda: statuses("outbox") == ["sent", "sent"])
                    assert statuses("inbox") == ["done", "done"]
                    assert len(requests) == 2
                    return
                await asyncio.wait_for(model_entered.wait(), timeout=5)
                if mode in {"roundtrip", "crash"}:
                    await peer.send(json.dumps(packet(1)))
                    await peer.send(json.dumps(packet(2)))
                    await until(lambda: len(statuses("inbox")) == 2)
                    assert statuses("inbox") == ["processing", "pending"]
                    assert len(requests) == 1
                if mode == "crash":
                    # Skip all finally blocks and SQLite/lock cleanup. On Windows, killing
                    # Popen.pid can kill only the venv launcher, leaving Python running.
                    os._exit(73)
                release_model.set()
                action = await receive(peer)
                if mode == "lost-receipt":
                    await until(lambda: statuses("outbox") == ["unknown"])
                    assert len(requests) == 1
                    return
                # Hold the first send receipt while the worker processes more inbound events.
                await peer.send(json.dumps(packet(3)))
                await until(lambda: statuses("inbox") == ["done", "done", "done"])
                assert len(requests) == 3
                assert statuses("outbox") == ["sending", "pending", "pending"]
                await acknowledge(peer, action, 501)
                for receipt in (502, 503):
                    await acknowledge(peer, await receive(peer), receipt)
                await until(lambda: statuses("outbox") == ["sent", "sent", "sent"])
                await peer.send(json.dumps(packet(1)))
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(peer.recv(), timeout=0.15)
                assert len(requests) == 3
        finally:
            gateway.server.should_exit = True
            if not serving.done():
                await asyncio.wait_for(serving, timeout=10)
            listener.close()
            for task in handlers:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
            assert runtime.database is None
            assert not gateway.adapter.connections
            assert not gateway.adapter.tasks
            with InstanceLock(config.data_dir):
                pass


def run_drill(directory, mode, *, expected_exit=0):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(directory), mode],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == expected_exit, result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["roundtrip", "daily-budget", "monthly-budget", "audit-failure"])
def test_complete_transport_chain_and_failure_supervision(tmp_path, mode):
    run_drill(tmp_path, mode)
    with Database(tmp_path / "data/ginko.sqlite3") as database:
        attempts = BudgetLedger(database, BudgetLimits(0, 0)).list_model_attempts()
        if mode == "roundtrip":
            assert len(attempts) == 3
            assert all(
                attempt.status == "settled"
                and attempt.outcome_code == "accepted"
                and attempt.actual_microusd == 27
                for attempt in attempts
            )
        elif mode == "audit-failure":
            assert len(attempts) == 1
            assert attempts[0].budget_status == "settled"
            assert attempts[0].outcome_code is None
            assert database.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        else:
            assert not attempts


def test_lost_receipt_stays_unknown_across_process_restart_and_redelivery(tmp_path):
    run_drill(tmp_path, "lost-receipt")
    run_drill(tmp_path, "resume-lost-receipt")
    with Database(tmp_path / "data/ginko.sqlite3") as database:
        assert [
            tuple(row)
            for row in database.connection.execute(
                "SELECT status, attempts, platform_message_id FROM outbox ORDER BY seq"
            )
        ] == [("unknown", 1, None), ("sent", 1, "502")]
        assert len(BudgetLedger(database, BudgetLimits(0, 0)).list_model_attempts()) == 2


def test_process_crash_recovers_inbox_without_losing_reservation_or_retry_bounds(tmp_path):
    run_drill(tmp_path, "crash", expected_exit=73)
    path = tmp_path / "data/ginko.sqlite3"
    with Database(path) as database:
        before = [
            dict(row) for row in database.connection.execute("SELECT * FROM inbox ORDER BY seq")
        ]
        ledger = BudgetLedger(database, BudgetLimits(0, 0))
        original = ledger.list_model_attempts()[0]
        assert original.status == original.budget_status == "reserved"
        assert original.outcome_code is None
        assert [row["status"] for row in before] == ["processing", "pending"]
    run_drill(tmp_path, "resume-crash")
    with Database(path) as database:
        after = [
            dict(row) for row in database.connection.execute("SELECT * FROM inbox ORDER BY seq")
        ]
        assert [row["expires_at"] for row in after] == [row["expires_at"] for row in before]
        assert [row["attempts"] for row in after] == [2, 1]
        assert [row["max_attempts"] for row in after] == [2, 2]
        ledger = BudgetLedger(database, BudgetLimits(0, 0))
        interrupted = ledger.model_attempt(original.operation_id)
        assert interrupted.status == "unknown"
        assert interrupted.budget_status == "reserved"
        assert interrupted.reserved_microusd == original.reserved_microusd
        assert interrupted.outcome_code == "interrupted"
        attempts = ledger.list_model_attempts(trace_id=original.trace_id)
        assert len(attempts) == 2
        assert attempts[1].operation_id != original.operation_id
        assert attempts[1].status == "settled"
        assert len(ledger.list_model_attempts()) == 3


if __name__ == "__main__":
    asyncio.run(exercise(Path(sys.argv[1]), sys.argv[2]))
