"""Time-dependent provider prices: settle at every rate a call may incur, reserve the highest."""

import asyncio
import json
import tomllib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from ginko.cli import main
from ginko.config import RuntimeSettings, TokenRates
from ginko.providers.chat import ChatClient, PromptMessage
from ginko.storage.budget import BudgetLedger, BudgetLimits

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"
OFF_PEAK = TokenRates(150_000, 3_000, 600_000)
PEAK = TokenRates(300_000, 6_000, 1_200_000)
MONDAY = datetime(2026, 9, 28, tzinfo=UTC)
SATURDAY = datetime(2026, 9, 26, tzinfo=UTC)
MESSAGES = (
    PromptMessage(role="system", content="Synthetic test identity."),
    PromptMessage(role="user", content="Synthetic test input."),
)


def at(day: datetime, clock: str) -> datetime:
    hours, minutes, *seconds = (int(part) for part in clock.split(":"))
    return day + timedelta(hours=hours, minutes=minutes, seconds=seconds[0] if seconds else 0)


@pytest.fixture
def raw():
    return tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))


def model(raw, **updates):
    raw["model"].update(updates)
    return RuntimeSettings.model_validate(raw).model


def window(**updates):
    return {
        "name": "window",
        "weekdays": [1, 2, 3, 4, 5, 6, 7],
        "utc_ranges": ["01:00-02:00"],
        "input_microusd_per_million_tokens": 50_000,
        "output_microusd_per_million_tokens": 100_000,
    } | updates


def test_example_documents_deepseek_peak_and_off_peak_prices(raw):
    settings = model(raw)
    assert settings.base_rates == OFF_PEAK
    assert [item.name for item in settings.rate_windows] == ["peak"]
    assert settings.rate_windows[0].rates == PEAK


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (at(MONDAY, "02:00"), at(MONDAY, "02:00:30")),
        (at(MONDAY, "01:00"), at(MONDAY, "01:00:05")),
        (at(MONDAY, "09:59:30"), at(MONDAY, "09:59:59")),
        (at(MONDAY + timedelta(days=4), "07:00"), at(MONDAY + timedelta(days=4), "07:01")),
    ],
)
def test_call_inside_peak_hours_settles_at_peak_rates(raw, start, end):
    assert model(raw).rates_between(start, end) == PEAK


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (at(MONDAY, "00:00"), at(MONDAY, "00:59:59")),
        (at(MONDAY, "04:00"), at(MONDAY, "05:59:59")),
        (at(MONDAY, "10:00"), at(MONDAY, "23:59:59")),
        (at(SATURDAY, "02:00"), at(SATURDAY, "02:01")),
        (at(SATURDAY + timedelta(days=1), "07:00"), at(SATURDAY + timedelta(days=1), "07:01")),
    ],
)
def test_weekday_gaps_and_weekends_settle_at_off_peak_rates(raw, start, end):
    assert model(raw).rates_between(start, end) == OFF_PEAK


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (at(MONDAY, "00:59:50"), at(MONDAY, "01:00:10")),
        (at(MONDAY, "03:59:50"), at(MONDAY, "04:00:10")),
        (at(MONDAY, "00:59:30"), at(MONDAY, "01:00")),
        (at(MONDAY, "05:59"), at(MONDAY, "06:01")),
    ],
)
def test_call_touching_a_peak_boundary_settles_at_peak_rates(raw, start, end):
    assert model(raw).rates_between(start, end) == PEAK


def test_window_closing_instant_is_not_part_of_the_window(raw):
    assert model(raw).rates_between(at(MONDAY, "04:00"), at(MONDAY, "04:00:01")) == OFF_PEAK


def test_cheaper_window_applies_only_when_it_covers_the_whole_call(raw):
    settings = model(raw, rate_windows=[window()])
    discount = TokenRates(50_000, 50_000, 100_000)
    assert settings.rates_between(at(MONDAY, "01:10"), at(MONDAY, "01:11")) == discount
    straddling = settings.rates_between(at(MONDAY, "01:59:50"), at(MONDAY, "02:00:10"))
    assert straddling == TokenRates.highest([OFF_PEAK, discount])


def test_adjacent_ranges_cover_a_call_without_falling_back_to_base(raw):
    settings = model(raw, rate_windows=[window(utc_ranges=["01:00-02:00", "02:00-03:00"])])
    rates = settings.rates_between(at(MONDAY, "01:59:50"), at(MONDAY, "02:00:10"))
    assert rates == TokenRates(50_000, 50_000, 100_000)


def test_windows_follow_utc_dates_across_midnight(raw):
    sunday_to_monday = (at(MONDAY, "00:00") - timedelta(seconds=10), at(MONDAY, "00:00:10"))
    discount = TokenRates(50_000, 50_000, 100_000)
    mixed = TokenRates.highest([OFF_PEAK, discount])
    monday_only = model(raw, rate_windows=[window(weekdays=[1], utc_ranges=["00:00-01:00"])])
    assert monday_only.rates_between(*sunday_to_monday) == mixed
    assert monday_only.rates_between(at(MONDAY, "00:00"), at(MONDAY, "00:01")) == discount
    sunday_only = (
        at(MONDAY, "00:00") - timedelta(minutes=5),
        at(MONDAY, "00:00") - timedelta(minutes=4),
    )
    assert monday_only.rates_between(*sunday_only) == OFF_PEAK

    sunday_night = model(raw, rate_windows=[window(weekdays=[7], utc_ranges=["23:00-24:00"])])
    assert sunday_night.rates_between(*sunday_to_monday) == mixed
    assert sunday_night.rates_between(*sunday_only) == discount
    peak_late = model(
        raw,
        rate_windows=[
            window(
                weekdays=[7],
                utc_ranges=["23:00-24:00"],
                input_microusd_per_million_tokens=900_000,
                output_microusd_per_million_tokens=900_000,
            )
        ],
    )
    assert peak_late.rates_between(*sunday_to_monday) == TokenRates(900_000, 900_000, 900_000)


def test_excluded_dates_settle_at_base_rates(raw):
    holiday = datetime(2026, 10, 1, tzinfo=UTC)
    raw["model"]["rate_windows"][0]["excluded_dates"] = [holiday.date()]
    settings = model(raw)
    assert settings.rates_between(at(holiday, "02:00"), at(holiday, "02:01")) == OFF_PEAK
    next_day = holiday + timedelta(days=1)
    assert settings.rates_between(at(next_day, "02:00"), at(next_day, "02:01")) == PEAK


def test_offset_timestamps_are_converted_to_utc(raw):
    beijing = timezone(timedelta(hours=8))
    start = datetime(2026, 9, 28, 10, 0, tzinfo=beijing)
    assert model(raw).rates_between(start, start + timedelta(seconds=5)) == PEAK


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 9, 28, 2), datetime(2026, 9, 28, 2, 1)),
        (at(MONDAY, "02:01"), at(MONDAY, "02:00")),
    ],
)
def test_naive_or_reversed_intervals_are_rejected(raw, start, end):
    with pytest.raises(ValueError, match="billing interval"):
        model(raw).rates_between(start, end)


def test_configs_without_windows_keep_flat_rates(raw):
    raw["model"].pop("rate_windows")
    settings = model(raw)
    assert settings.rate_windows == ()
    assert settings.rates_between(at(MONDAY, "02:00"), at(MONDAY, "02:01")) == OFF_PEAK
    assert settings.reservation_microusd == 2400 + 600


def test_window_without_cache_rate_charges_cached_input_at_its_input_rate(raw):
    settings = model(raw, rate_windows=[window()])
    assert settings.rate_windows[0].rates.cached_input == 50_000


def test_reservation_uses_the_highest_rate_of_every_category(raw):
    # 16000 input tokens at 0.3 USD/M plus 1000 output tokens at 1.2 USD/M.
    assert model(raw).reservation_microusd == 4800 + 1200
    cheaper = model(raw, rate_windows=[window()])
    assert cheaper.reservation_microusd == 2400 + 600
    mixed = model(
        raw,
        rate_windows=[
            window(input_microusd_per_million_tokens=400_000),
            window(name="output", output_microusd_per_million_tokens=2_000_000),
        ],
    )
    assert mixed.reservation_microusd == 6400 + 2000


@pytest.mark.parametrize(
    "updates",
    [
        {"utc_ranges": ["01:00-01:00"]},
        {"utc_ranges": ["04:00-01:00"]},
        {"utc_ranges": ["1:00-02:00"]},
        {"utc_ranges": ["24:00-24:00"]},
        {"utc_ranges": ["01:00-24:01"]},
        {"utc_ranges": ["25:00-26:00"]},
        {"utc_ranges": []},
        {"weekdays": []},
        {"weekdays": [0]},
        {"weekdays": [8]},
        {"weekdays": [1, 1]},
        {"weekdays": ["1"]},
        {"weekdays": [True]},
        {"excluded_dates": ["2026-10-01"]},
        {"excluded_dates": [datetime(2026, 10, 1).date()] * 2},
        {"input_microusd_per_million_tokens": 0},
        {"output_microusd_per_million_tokens": -1},
        {"cached_input_microusd_per_million_tokens": -1},
        {"timezone": "Asia/Shanghai"},
        {"name": ""},
    ],
)
def test_invalid_windows_are_rejected(raw, updates):
    raw["model"]["rate_windows"] = [window(**updates)]
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate(raw)


def test_window_names_must_be_unique(raw):
    raw["model"]["rate_windows"] = [window(), window(utc_ranges=["05:00-06:00"])]
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate(raw)


def test_check_config_reports_window_count_and_highest_reservation(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.local.toml"
    path.write_bytes(EXAMPLE.read_bytes())
    monkeypatch.setenv("GINKO_ONEBOT_ACCESS_TOKEN", "synthetic-onebot-token")
    monkeypatch.setenv("GINKO_MODEL_API_KEY", "synthetic-model-key")
    assert main(["check-config", str(path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["rate_windows"] == 1
    assert summary["attempt_reservation_microusd"] == 6000


class Clock:
    def __init__(self, *instants: datetime) -> None:
        self.instants = list(instants)

    def __call__(self) -> datetime:
        return self.instants.pop(0) if len(self.instants) > 1 else self.instants[0]


def settle(database, raw, clock: Clock, usage: dict[str, int]) -> tuple[int, int]:
    settings = model(raw)
    account = BudgetLedger(database, BudgetLimits(100_000, 1_000_000))
    data = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Synthetic reply."},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }

    def handle(_):
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=httpx.ByteStream(json.dumps(data).encode()),
        )

    async def run():
        async with ChatClient(
            settings,
            SecretStr("synthetic-model-key"),
            account,
            transport=httpx.MockTransport(handle),
            clock=clock,
        ) as client:
            return await client.complete(MESSAGES, trace_id=uuid4())

    reply = asyncio.run(run())
    attempt = account.model_attempt(reply.operation_id)
    assert attempt.actual_microusd == reply.cost_microusd
    return reply.cost_microusd, attempt.reserved_microusd


USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 100,
    "total_tokens": 1100,
    "prompt_cache_hit_tokens": 400,
    "prompt_cache_miss_tokens": 600,
}


@pytest.mark.parametrize(
    ("instants", "rates"),
    [
        ((at(MONDAY, "02:00"), at(MONDAY, "02:00:02")), PEAK),
        ((at(SATURDAY, "02:00"), at(SATURDAY, "02:00:02")), OFF_PEAK),
        ((at(MONDAY, "00:59:59"), at(MONDAY, "01:00:01")), PEAK),
        ((at(MONDAY, "03:59:59"), at(MONDAY, "04:00:01")), PEAK),
    ],
)
def test_model_attempt_settles_at_the_rates_of_its_request_interval(database, raw, instants, rates):
    cost, reserved = settle(database, raw, Clock(*instants), USAGE)
    assert cost == rates.cost(1000, 400, 100)
    assert reserved == 6000


def test_peak_and_off_peak_costs_for_the_same_usage(database, raw):
    # Off-peak: ceil((600*0.15 + 400*0.003) USD) = 92; output 100*0.6 = 60.
    off_peak, _ = settle(database, raw, Clock(at(SATURDAY, "02:00")), USAGE)
    peak, _ = settle(database, raw, Clock(at(MONDAY, "02:00")), USAGE)
    assert (off_peak, peak) == (92 + 60, 183 + 120)
