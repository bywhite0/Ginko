import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest

from ginko.storage.budget import (
    BudgetExceededError,
    BudgetLedger,
    BudgetLimits,
    BudgetOverrunError,
)
from ginko.storage.database import Database


def test_every_purpose_shares_hard_limits(database, now):
    ledger = BudgetLedger(database, BudgetLimits(500, 1000))
    ledger.reserve("chat:1", "chat", 300, now=now)
    for purpose in ("memory", "judge", "status", "reflection", "tool", "retry"):
        with pytest.raises(BudgetExceededError):
            ledger.reserve(purpose, purpose, 201, now=now)
    ledger.settle("chat:1", 100)
    ledger.reserve("judge:1", "judge", 400, now=now)
    with pytest.raises(BudgetExceededError):
        ledger.reserve("chat:2", "chat", 1, now=now)


def test_unresolved_cost_and_month_limit_survive_restart(tmp_path, now):
    path = tmp_path / "ledger.sqlite3"
    with Database(path) as database:
        ledger = BudgetLedger(database, BudgetLimits(500, 600))
        ledger.reserve("unknown-call", "chat", 400, now=now)
    with Database(path) as database:
        ledger = BudgetLedger(database, BudgetLimits(500, 600))
        with pytest.raises(BudgetExceededError):
            ledger.reserve("next-day", "judge", 201, now=now + timedelta(days=1))


def test_one_reservation_cannot_authorize_two_calls(database, now):
    ledger = BudgetLedger(database, BudgetLimits(500, 1000))
    ledger.reserve("attempt:1", "chat", 100, now=now)
    with pytest.raises(ValueError):
        ledger.reserve("attempt:1", "chat", 100, now=now)
    ledger.settle("attempt:1", 50)
    ledger.settle("attempt:1", 50)
    with pytest.raises(ValueError):
        ledger.settle("attempt:1", 40)
    with pytest.raises(ValueError):
        ledger.reserve("attempt:1", "chat", 100, now=now)


def test_overrun_records_actual_charge_before_raising(database, now):
    ledger = BudgetLedger(database, BudgetLimits(500, 1000))
    ledger.reserve("underestimated", "chat", 400, now=now)
    with pytest.raises(BudgetOverrunError):
        ledger.settle("underestimated", 600)
    with pytest.raises(BudgetExceededError):
        ledger.reserve("another", "chat", 1, now=now)


def test_cancel_before_call_releases_budget(database, now):
    ledger = BudgetLedger(database, BudgetLimits(500, 1000))
    ledger.reserve("cancelled", "chat", 500, now=now)
    ledger.cancel_before_call("cancelled")
    ledger.reserve("new", "chat", 500, now=now)


@pytest.mark.parametrize("amount", [-1, 0, 0.5, True])
def test_reservations_require_positive_integer_amount(database, now, amount):
    ledger = BudgetLedger(database, BudgetLimits(500, 1000))
    with pytest.raises(ValueError):
        ledger.reserve("invalid", "chat", amount, now=now)


def test_concurrent_connections_cannot_overbook(tmp_path, now):
    path = tmp_path / "concurrent.sqlite3"
    with Database(path):
        pass
    ready = Barrier(2)

    def reserve(operation_id):
        with Database(path) as database:
            ledger = BudgetLedger(database, BudgetLimits(500, 1000))
            ready.wait(timeout=10)
            try:
                ledger.reserve(operation_id, "chat", 300, now=now)
            except BudgetExceededError:
                return False
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(reserve, ("a", "b"))) == [False, True]


def test_model_attempt_audit_survives_restart_and_keeps_unknown_reserved(tmp_path, now):
    path = tmp_path / "attempts.sqlite3"
    with Database(path) as database:
        ledger = BudgetLedger(database, BudgetLimits(500, 1000))
        ledger.reserve("trace:attempt", "chat", 400, now=now, trace_id="trace")
        ledger.record_model_outcome("trace:attempt", "unknown_usage")
        record = ledger.model_attempt("trace:attempt")
        assert record.trace_id == "trace"
        assert record.status == "unknown"
        assert record.budget_status == "reserved"
        assert record.actual_microusd is None
    with Database(path) as database:
        ledger = BudgetLedger(database, BudgetLimits(500, 1000))
        records = ledger.list_model_attempts(trace_id="trace")
        assert len(records) == 1
        assert records[0].outcome_code == "unknown_usage"
        with pytest.raises(BudgetExceededError):
            ledger.reserve("next", "chat", 101, now=now)


def test_model_attempt_settlement_records_usage_and_outcome(database, now):
    ledger = BudgetLedger(database, BudgetLimits(500, 1000))
    ledger.reserve("trace:ok", "chat", 100, now=now, trace_id="trace")
    ledger.settle("trace:ok", 42, usage=(30, 10, 40))
    ledger.record_model_outcome("trace:ok", "accepted")
    record = ledger.model_attempt("trace:ok")
    assert record.status == "settled"
    assert record.budget_status == "settled"
    assert (record.prompt_tokens, record.completion_tokens, record.total_tokens) == (30, 10, 40)
    assert record.actual_microusd == 42
    assert record.outcome_code == "accepted"


def test_settlement_without_usage_keeps_budget_and_audit_aligned(database, now):
    ledger = BudgetLedger(database, BudgetLimits(500, 1000))
    ledger.reserve("attempt", "chat", 100, now=now, trace_id="trace")
    ledger.settle("attempt", 40)
    record = ledger.model_attempt("attempt")
    assert record.status == record.budget_status == "settled"
    assert record.prompt_tokens is record.outcome_code is None


@pytest.mark.parametrize(
    "changes",
    [
        {"actual": 41},
        {"usage": (29, 11, 40)},
        {"cached_prompt_tokens": 19},
        {"reasoning_tokens": 7},
    ],
)
def test_repeated_settlement_rejects_conflicting_evidence(database, now, changes):
    ledger = BudgetLedger(database, BudgetLimits(500, 1000))
    ledger.reserve("attempt", "chat", 100, now=now, trace_id="trace")
    settlement = dict(actual=40, usage=(30, 10, 40), cached_prompt_tokens=20, reasoning_tokens=8)
    ledger.settle("attempt", **settlement)
    ledger.record_model_outcome("attempt", "accepted")
    before = ledger.model_attempt("attempt")
    ledger.settle("attempt", **settlement)
    ledger.record_model_outcome("attempt", "accepted")
    with pytest.raises(ValueError):
        ledger.settle("attempt", **(settlement | changes))
    assert ledger.model_attempt("attempt") == before


@pytest.mark.parametrize(
    "details",
    [
        {"usage": (0, 0, 0)},
        {"usage": (30, 10, 39)},
        {"usage": (30, True, 31)},
        {"usage": (30, 10, 40), "cached_prompt_tokens": 31},
        {"usage": (30, 10, 40), "reasoning_tokens": 11},
        {"cached_prompt_tokens": 0},
    ],
)
def test_invalid_usage_cannot_change_budget(database, now, details):
    ledger = BudgetLedger(database, BudgetLimits(500, 1000))
    ledger.reserve("attempt", "chat", 100, now=now, trace_id="trace")
    before = ledger.model_attempt("attempt")
    with pytest.raises(ValueError):
        ledger.settle("attempt", 40, **details)
    assert ledger.model_attempt("attempt") == before


def test_model_outcomes_preserve_terminal_evidence(database, now):
    ledger = BudgetLedger(database, BudgetLimits(500, 1000))
    with pytest.raises(KeyError):
        ledger.record_model_outcome("missing", "timeout")
    ledger.reserve("attempt", "chat", 100, now=now, trace_id="trace")
    with pytest.raises(ValueError):
        ledger.record_model_outcome("attempt", "accepted")
    with pytest.raises(ValueError):
        ledger.record_model_outcome("attempt", "exception with private payload")
    ledger.record_model_outcome("attempt", "timeout")
    before = ledger.model_attempt("attempt")
    with pytest.raises(ValueError):
        ledger.record_model_outcome("attempt", "transport_error")
    with pytest.raises(ValueError):
        ledger.cancel_before_call("attempt")
    assert ledger.model_attempt("attempt") == before
    # Later verified billing can settle the charge without rewriting the call result.
    ledger.settle("attempt", 40, usage=(30, 10, 40))
    assert ledger.model_attempt("attempt").outcome_code == "timeout"
    assert ledger.model_attempt("attempt").budget_status == "settled"


def test_cancelled_attempt_cannot_be_revived(database, now):
    ledger = BudgetLedger(database, BudgetLimits(500, 1000))
    ledger.reserve("attempt", "chat", 100, now=now, trace_id="trace")
    ledger.cancel_before_call("attempt")
    before = ledger.model_attempt("attempt")
    assert before.status == before.budget_status == "cancelled"
    assert before.outcome_code == "cancelled_before_call"
    with pytest.raises(ValueError):
        ledger.record_model_outcome("attempt", "timeout")
    with pytest.raises(ValueError):
        ledger.settle("attempt", 40)
    assert ledger.recover_interrupted_model_attempts() == 0
    assert ledger.model_attempt("attempt") == before


def test_audit_and_budget_writes_are_atomic(database, now):
    ledger = BudgetLedger(database, BudgetLimits(500, 1000))
    database.connection.execute("""CREATE TRIGGER reject_audit BEFORE INSERT ON model_attempts
        BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.reserve("attempt", "chat", 100, now=now, trace_id="trace")
    assert database.connection.execute("SELECT COUNT(*) FROM budget").fetchone()[0] == 0
    database.connection.execute("DROP TRIGGER reject_audit")
    ledger.reserve("attempt", "chat", 100, now=now, trace_id="trace")
    before = ledger.model_attempt("attempt")
    database.connection.execute("""CREATE TRIGGER reject_audit BEFORE UPDATE ON model_attempts
        BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.settle("attempt", 40, usage=(30, 10, 40))
    assert ledger.model_attempt("attempt") == before
    with pytest.raises(sqlite3.IntegrityError):
        ledger.cancel_before_call("attempt")
    assert ledger.model_attempt("attempt") == before


def test_recovery_preserves_billing_and_completed_results(database, now):
    ledger = BudgetLedger(database, BudgetLimits(500, 1000))
    for operation in ("in_flight", "billed", "unknown", "done"):
        ledger.reserve(operation, "chat", 100, now=now, trace_id="trace")
    ledger.settle("billed", 40, usage=(30, 10, 40))
    ledger.record_model_outcome("unknown", "timeout")
    ledger.settle("done", 40, usage=(30, 10, 40))
    ledger.record_model_outcome("done", "accepted")
    assert ledger.recover_interrupted_model_attempts() == 2
    assert ledger.recover_interrupted_model_attempts() == 0
    assert ledger.model_attempt("in_flight").status == "unknown"
    assert ledger.model_attempt("in_flight").budget_status == "reserved"
    billed = ledger.model_attempt("billed")
    assert billed.outcome_code == "interrupted"
    assert billed.status == billed.budget_status == "settled"
    assert billed.actual_microusd == 40
    assert billed.total_tokens == 40
    assert ledger.model_attempt("unknown").outcome_code == "timeout"
    assert ledger.model_attempt("done").outcome_code == "accepted"
    with pytest.raises(BudgetExceededError):
        ledger.reserve("too_large", "chat", 221, now=now)
