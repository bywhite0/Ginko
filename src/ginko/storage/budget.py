"""Every paid attempt must reserve its worst-case cost before starting."""

from dataclasses import dataclass
from datetime import UTC, datetime

from ginko.storage.database import Database, timestamp


class BudgetExceededError(RuntimeError):
    pass


class BudgetOverrunError(RuntimeError):
    """Actual cost exceeded its reservation; the actual charge is still recorded."""


@dataclass(frozen=True)
class BudgetLimits:
    # Integer micro-USD avoids rounding away small inference costs.
    daily_microusd: int
    monthly_microusd: int

    def __post_init__(self) -> None:
        for value in (self.daily_microusd, self.monthly_microusd):
            if type(value) is not int or value < 0:
                raise ValueError("budget limits must be non-negative integer micro-USD")


def validate_amount(value: int, *, positive: bool = False) -> None:
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError("cost must be integer micro-USD in the permitted range")


class BudgetLedger:
    def __init__(self, database: Database, limits: BudgetLimits) -> None:
        self.database = database
        self.limits = limits

    def reserve(self, operation_id: str, purpose: str, amount: int, *, now: datetime) -> None:
        validate_amount(amount, positive=True)
        timestamp(now)
        if not operation_id or not purpose:
            raise ValueError("operation_id and purpose are required")
        day = now.astimezone(UTC).date().isoformat()
        month = day[:7]
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM budget WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if existing is not None:
                # Even an unresolved reservation cannot authorize a second call.
                # Reconcile that attempt or reserve a new ID and its full cost.
                raise ValueError("operation_id already belongs to an existing attempt")
            for column, period, limit in (
                ("day", day, self.limits.daily_microusd),
                ("month", month, self.limits.monthly_microusd),
            ):
                total = connection.execute(
                    f"""SELECT COALESCE(SUM(CASE WHEN status = 'reserved' THEN reserved
                    WHEN status = 'settled' THEN actual ELSE 0 END), 0)
                    FROM budget WHERE {column} = ?""",
                    (period,),
                ).fetchone()[0]
                if total + amount > limit:
                    raise BudgetExceededError(f"{column} budget exhausted")
            connection.execute(
                """INSERT INTO budget (operation_id, purpose, day, month, reserved, status)
                VALUES (?, ?, ?, ?, ?, 'reserved')""",
                (operation_id, purpose, day, month, amount),
            )

    def settle(self, operation_id: str, actual: int) -> None:
        validate_amount(actual)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM budget WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            if row["status"] == "settled" and row["actual"] == actual:
                return
            if row["status"] != "reserved":
                raise ValueError("only a live reservation can be settled")
            connection.execute(
                "UPDATE budget SET actual = ?, status = 'settled' WHERE operation_id = ?",
                (actual, operation_id),
            )
            overrun = actual > row["reserved"]
        if overrun:
            # Raise after COMMIT so unexpectedly large real costs never disappear.
            raise BudgetOverrunError("actual charge recorded; reservation was too small")

    def cancel_before_call(self, operation_id: str) -> None:
        """Release only when no external call started; unknown usage stays reserved."""
        with self.database.transaction() as connection:
            changed = connection.execute(
                """UPDATE budget SET status = 'cancelled'
                WHERE operation_id = ? AND status = 'reserved'""",
                (operation_id,),
            ).rowcount
            if changed != 1:
                raise ValueError("no live reservation to cancel")
