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
