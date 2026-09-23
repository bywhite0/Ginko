import asyncio
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from ginko.config import RuntimeSettings
from ginko.providers.chat import (
    ChatClient,
    ModelCallError,
    PromptMessage,
    ProviderContractError,
)
from ginko.storage.budget import BudgetExceededError, BudgetLedger, BudgetLimits, BudgetOverrunError
from ginko.storage.database import Database

MESSAGES = (
    PromptMessage(role="system", content="Synthetic test identity."),
    PromptMessage(role="user", content="Synthetic test input."),
)
SECRET = "synthetic-api-key-never-log"


@pytest.fixture
def settings():
    raw = tomllib.loads((Path(__file__).parents[1] / "config.example.toml").read_text())
    raw["model"].update(
        max_input_tokens=1000,
        max_output_tokens=100,
        input_microusd_per_million_tokens=1_000_000,
        cached_input_microusd_per_million_tokens=None,
        output_microusd_per_million_tokens=2_000_000,
    )
    return RuntimeSettings.model_validate(raw).model


def completion(**updates):
    return {
        "id": "synthetic-completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Synthetic reply."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
    } | updates


def response(data, *, status=200, headers=None):
    body = json.dumps(data).encode()
    return httpx.Response(
        status,
        headers={"Content-Type": "application/json"} | (headers or {}),
        stream=httpx.ByteStream(body),
    )


def ledger(database, daily=10_000, monthly=100_000):
    return BudgetLedger(database, BudgetLimits(daily, monthly))


def rows(database):
    return database.connection.execute("SELECT * FROM budget ORDER BY rowid").fetchall()


def client(settings, account, handler):
    return ChatClient(settings, SecretStr(SECRET), account, transport=httpx.MockTransport(handler))


def test_reservation_is_committed_before_http_and_actual_usage_settles(tmp_path, settings):
    path = tmp_path / "model.sqlite3"
    requests = []
    with Database(path) as database:

        def handle(request):
            requests.append(request)
            # A second connection sees the reservation before any external attempt starts.
            with Database(path) as observer:
                assert rows(observer)[0]["status"] == "reserved"
                assert rows(observer)[0]["reserved"] == 1200
            payload = json.loads(request.content)
            assert payload["model"] == settings.model
            assert payload["max_tokens"] == 100
            assert payload["n"] == 1
            assert payload["stream"] is False
            assert request.headers["authorization"] == "Bearer " + SECRET
            assert request.headers["accept-encoding"] == "identity"
            return response(completion())

        async def run():
            trace = uuid4()
            async with client(settings, ledger(database), handle) as model:
                result = await model.complete(MESSAGES, trace_id=trace)
                assert result.text == "Synthetic reply."
                assert result.usage.total_tokens == 40
                assert result.cost_microusd == 50
                assert result.operation_id.startswith(f"{trace}:")
            row = rows(database)[0]
            assert row["operation_id"] == result.operation_id
            assert row["status"] == "settled"
            assert row["actual"] == 50
            assert len(requests) == 1

        asyncio.run(run())


@pytest.mark.parametrize("daily,monthly", [(1199, 10000), (10000, 1199), (0, 0)])
def test_either_hard_limit_prevents_all_network_calls(database, settings, daily, monthly):
    async def run():
        def handle(request):
            pytest.fail("HTTP called without enough budget")

        async with client(settings, ledger(database, daily, monthly), handle) as model:
            with pytest.raises(BudgetExceededError):
                await model.complete(MESSAGES, trace_id=uuid4())
        assert rows(database) == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "messages", [(), MESSAGES * 9, (PromptMessage(role="user", content="中" * 500),)]
)
def test_invalid_or_oversized_prompt_is_rejected_before_reserving(database, settings, messages):
    async def run():
        def handle(request):
            pytest.fail("HTTP called for an inadmissible prompt")

        async with client(settings, ledger(database), handle) as model:
            with pytest.raises(ModelCallError):
                await model.complete(messages, trace_id=uuid4())
        assert rows(database) == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        {"prompt_tokens": 0, "completion_tokens": 10, "total_tokens": 10},
        {"prompt_tokens": -1, "completion_tokens": 10, "total_tokens": 9},
        {"prompt_tokens": True, "completion_tokens": 10, "total_tokens": 11},
        {"prompt_tokens": "30", "completion_tokens": 10, "total_tokens": 40},
        {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 99},
        {"prompt_tokens": 30, "completion_tokens": 10},
        {
            "prompt_tokens": 30,
            "completion_tokens": 10,
            "total_tokens": 40,
            "completion_tokens_details": {"reasoning_tokens": 101},
        },
        {
            "prompt_tokens": 30,
            "completion_tokens": 10,
            "total_tokens": 40,
            "prompt_tokens_details": {"cached_tokens": 99},
        },
        {
            "prompt_tokens": 30,
            "completion_tokens": 10,
            "total_tokens": 40,
            "prompt_cache_hit_tokens": 29,
            "prompt_cache_miss_tokens": 2,
        },
        {
            "prompt_tokens": 30,
            "completion_tokens": 10,
            "total_tokens": 40,
            "completion_tokens_details": {"reasoning_tokens": True},
        },
    ],
)
def test_missing_or_untrustworthy_usage_keeps_full_reservation(database, settings, usage):
    async def run():
        async with client(
            settings, ledger(database), lambda _: response(completion(usage=usage))
        ) as model:
            with pytest.raises(ModelCallError, match="unknown_usage") as error:
                await model.complete(MESSAGES, trace_id=uuid4())
            assert error.value.retryable
        assert rows(database)[0]["status"] == "reserved"
        assert rows(database)[0]["actual"] is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "choice,code,retryable",
    [
        (
            {"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"},
            "empty_reply",
            True,
        ),
        (
            {
                "message": {"role": "assistant", "content": None, "refusal": "no"},
                "finish_reason": "stop",
            },
            "refused",
            False,
        ),
        (
            {"message": {"role": "assistant", "content": "partial"}, "finish_reason": "length"},
            "incomplete_reply",
            False,
        ),
        (
            {
                "message": {"role": "assistant", "content": "blocked"},
                "finish_reason": "content_filter",
            },
            "refused",
            False,
        ),
        (
            {
                "message": {"role": "assistant", "content": "text", "tool_calls": [{}]},
                "finish_reason": "stop",
            },
            "incomplete_reply",
            False,
        ),
        (
            {"message": {"role": "user", "content": "wrong"}, "finish_reason": "stop"},
            "invalid_message",
            True,
        ),
    ],
)
def test_unusable_responses_still_settle_valid_usage(database, settings, choice, code, retryable):
    async def run():
        async with client(
            settings, ledger(database), lambda _: response(completion(choices=[choice]))
        ) as model:
            with pytest.raises(ModelCallError, match=code) as error:
                await model.complete(MESSAGES, trace_id=uuid4())
            assert error.value.retryable is retryable
        assert rows(database)[0]["status"] == "settled"
        assert rows(database)[0]["actual"] == 50

    asyncio.run(run())


@pytest.mark.parametrize("with_usage", [False, True])
@pytest.mark.parametrize("status,retryable", [(401, False), (429, True), (503, True)])
def test_http_errors_only_settle_when_usage_is_present(
    database, settings, with_usage, status, retryable
):
    async def run():
        data = completion() if with_usage else {"error": {"message": SECRET}}
        async with client(
            settings, ledger(database), lambda _: response(data, status=status)
        ) as model:
            with pytest.raises(ModelCallError, match="http_error") as error:
                await model.complete(MESSAGES, trace_id=uuid4())
            assert error.value.retryable is retryable
            assert SECRET not in str(error.value)
        assert rows(database)[0]["status"] == ("settled" if with_usage else "reserved")

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,body,retryable",
    [
        (429, b"", True),
        (408, b"<html>timeout</html>", True),
        (401, b"[]", False),
        (503, b"[]", True),
    ],
)
def test_http_retry_classification_does_not_depend_on_body_shape(
    database, settings, status, body, retryable
):
    async def run():
        def handle(request):
            return httpx.Response(status, stream=httpx.ByteStream(body))

        async with client(settings, ledger(database), handle) as model:
            with pytest.raises(ModelCallError) as error:
                await model.complete(MESSAGES, trace_id=uuid4())
            assert error.value.retryable is retryable
        assert rows(database)[0]["status"] == "reserved"

    asyncio.run(run())


def test_explicit_retries_have_new_reservations_and_never_release_unknown_spend(database, settings):
    async def run():
        count = 0

        def handle(request):
            nonlocal count
            count += 1
            return response({"error": "synthetic"}, status=503)

        async with client(settings, ledger(database, daily=2400), handle) as model:
            trace = uuid4()
            for _ in range(2):
                with pytest.raises(ModelCallError):
                    await model.complete(MESSAGES, trace_id=trace)
            with pytest.raises(BudgetExceededError):
                await model.complete(MESSAGES, trace_id=trace)
        assert count == 2
        saved = rows(database)
        assert len({row["operation_id"] for row in saved}) == 2
        assert all(row["status"] == "reserved" for row in saved)
        assert all(row["operation_id"].startswith(f"{trace}:") for row in saved)

    asyncio.run(run())


@pytest.mark.parametrize(
    "kind", ["timeout", "transport", "malformed", "sse", "oversized", "encoded"]
)
def test_unknown_results_remain_reserved_and_do_not_leak_payloads(database, settings, kind, caplog):
    async def run():
        def handle(request):
            if kind == "timeout":
                raise httpx.ReadTimeout(SECRET, request=request)
            if kind == "transport":
                raise httpx.ConnectError(SECRET, request=request)
            if kind == "sse":
                return response(completion(), headers={"Content-Type": "text/event-stream"})
            if kind == "oversized":
                return response({"body": "x" * 1_000_001})
            if kind == "encoded":
                return response(completion(), headers={"Content-Encoding": "gzip"})
            return httpx.Response(200, stream=httpx.ByteStream(b"{invalid " + SECRET.encode()))

        async with client(settings, ledger(database), handle) as model:
            with pytest.raises(ModelCallError) as error:
                await model.complete(MESSAGES, trace_id=uuid4())
            assert SECRET not in str(error.value)
        assert rows(database)[0]["status"] == "reserved"
        assert SECRET not in caplog.text

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_incomplete_body_timeout_or_cancellation_closes_transport_and_survives_restart(
    tmp_path, settings, cancel
):
    path = tmp_path / "unknown.sqlite3"
    settings = settings.model_copy(update={"timeout_seconds": 0.05})

    async def run(database):
        begun = asyncio.Event()

        class PartialBody(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b'{"choices":['
                begun.set()
                await asyncio.sleep(10)

            async def aclose(self):
                self.closed = True

        body = PartialBody()
        async with client(
            settings, ledger(database), lambda _: httpx.Response(200, stream=body)
        ) as model:
            call = asyncio.create_task(model.complete(MESSAGES, trace_id=uuid4()))
            await begun.wait()
            if cancel:
                call.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await call
            else:
                with pytest.raises(ModelCallError, match="timeout"):
                    await call
            assert body.closed

    with Database(path) as database:
        asyncio.run(run(database))
    with Database(path) as reopened:
        assert rows(reopened)[0]["status"] == "reserved"
        account = ledger(reopened, daily=1200)
        with pytest.raises(BudgetExceededError):
            account.reserve("next", "reply", 1, now=datetime.now(UTC))


@pytest.mark.parametrize(
    "output,exception", [(0, ProviderContractError), (100, BudgetOverrunError)]
)
def test_provider_overrun_records_cost_before_stopping(database, settings, output, exception):
    usage = {"prompt_tokens": 1001, "completion_tokens": output, "total_tokens": 1001 + output}
    data = completion(usage=usage)
    if output == 0:
        data["choices"][0]["message"]["content"] = ""

    async def run():
        async with client(settings, ledger(database), lambda _: response(data)) as model:
            with pytest.raises(exception):
                await model.complete(MESSAGES, trace_id=uuid4())
        assert rows(database)[0]["status"] == "settled"
        assert rows(database)[0]["actual"] == 1001 + 2 * output

    asyncio.run(run())


def test_cache_price_uses_only_validated_hit_counts_and_reserves_worst_case(database, settings):
    priced = settings.model_copy(
        update={
            "input_microusd_per_million_tokens": 220_000,
            "cached_input_microusd_per_million_tokens": 7_000,
            "output_microusd_per_million_tokens": 660_000,
        }
    )
    data = completion()
    data["usage"]["prompt_cache_hit_tokens"] = 20

    async def run():
        async with client(priced, ledger(database), lambda _: response(data)) as model:
            result = await model.complete(MESSAGES, trace_id=uuid4())
        # Input: ceil((10*220000 + 20*7000)/1e6)=3; output: ceil(10*660000/1e6)=7.
        assert result.cost_microusd == 10
        assert rows(database)[0]["reserved"] == 286

    asyncio.run(run())


def test_nonempty_generated_text_with_zero_output_usage_stays_reserved(database, settings):
    data = completion(usage={"prompt_tokens": 30, "completion_tokens": 0, "total_tokens": 30})

    async def run():
        async with client(settings, ledger(database), lambda _: response(data)) as model:
            with pytest.raises(ModelCallError, match="unknown_usage"):
                await model.complete(MESSAGES, trace_id=uuid4())
        assert rows(database)[0]["status"] == "reserved"

    asyncio.run(run())


def test_redirects_are_not_followed_and_do_not_forward_credentials(database, settings):
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return response({}, status=307, headers={"Location": "https://elsewhere.invalid/"})

    async def run():
        async with client(settings, ledger(database), handle) as model:
            with pytest.raises(ModelCallError):
                await model.complete(MESSAGES, trace_id=uuid4())
        assert calls == [settings.base_url + "/chat/completions"]
        assert rows(database)[0]["status"] == "reserved"

    asyncio.run(run())


def test_body_and_reasoning_tokens_are_charged_together_without_assuming_cache_discounts(
    database, settings
):
    usage = completion()["usage"] | {
        "prompt_tokens_details": {"cached_tokens": 20},
        "completion_tokens_details": {"reasoning_tokens": 8},
    }

    async def run():
        async with client(
            settings, ledger(database), lambda _: response(completion(usage=usage))
        ) as model:
            result = await model.complete(MESSAGES, trace_id=uuid4())
        assert result.cost_microusd == 50

    asyncio.run(run())


def test_real_http_nonstreaming_request_with_committed_budget(database, settings):
    async def run():
        captured = []

        async def handle(reader, writer):
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    int(line.partition(b":")[2])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                captured.append(json.loads(await reader.readexactly(length)))
                assert rows(database)[0]["status"] == "reserved"
                body = json.dumps(completion()).encode()
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

        async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            local = settings.model_copy(update={"base_url": f"http://127.0.0.1:{port}/v1"})
            async with ChatClient(local, SecretStr(SECRET), ledger(database)) as model:
                reply = await model.complete(MESSAGES, trace_id=uuid4())
            assert reply.text == "Synthetic reply."
            assert len(captured) == 1
            assert captured[0]["stream"] is False
        assert rows(database)[0]["status"] == "settled"

    asyncio.run(run())
