"""Every paid attempt must reserve its worst-case cost before starting."""

from dataclasses import dataclass
from datetime import UTC, datetime
from re import fullmatch

from ginko.storage.database import Database, timestamp


class BudgetExceededError(RuntimeError):
    pass


class BudgetOverrunError(RuntimeError):
    """Actual cost exceeded its reservation; the actual charge is still recorded."""


@dataclass(frozen=True)
class ModelAttemptRecord:
    """Durable, non-secret metadata for one model request."""

    operation_id: str
    trace_id: str
    budget_status: str
    status: str
    reserved_microusd: int
    actual_microusd: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_prompt_tokens: int | None
    reasoning_tokens: int | None
    outcome_code: str | None
    created_at: datetime
    updated_at: datetime


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

    def reserve(
        self,
        operation_id: str,
        purpose: str,
        amount: int,
        *,
        now: datetime,
        trace_id: str | None = None,
    ) -> None:
        validate_amount(amount, positive=True)
        timestamp(now)
        if not operation_id or not purpose:
            raise ValueError("operation_id and purpose are required")
        if trace_id is not None and not trace_id:
            raise ValueError("trace_id must be nonempty when provided")
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
            if trace_id is not None:
                created = timestamp(now)
                connection.execute(
                    """INSERT INTO model_attempts
                    (operation_id, trace_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?)""",
                    (operation_id, trace_id, created, created),
                )

    def settle(
        self,
        operation_id: str,
        actual: int,
        *,
        usage: tuple[int, int, int] | None = None,
        cached_prompt_tokens: int | None = None,
        reasoning_tokens: int | None = None,
    ) -> None:
        validate_amount(actual)
        if usage is not None:
            _validate_usage(usage, cached_prompt_tokens, reasoning_tokens)
        elif cached_prompt_tokens is not None or reasoning_tokens is not None:
            raise ValueError("token details require usage totals")
        now = datetime.now(UTC)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM budget WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            attempt = connection.execute(
                "SELECT * FROM model_attempts WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if usage is not None and attempt is None:
                raise ValueError("usage requires a model attempt reservation")
            if row["status"] == "settled" and row["actual"] == actual:
                if usage is not None and (
                    tuple(
                        attempt[name]
                        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
                    )
                    != usage
                    or (
                        cached_prompt_tokens is not None
                        and attempt["cached_prompt_tokens"] != cached_prompt_tokens
                    )
                    or (
                        reasoning_tokens is not None
                        and attempt["reasoning_tokens"] != reasoning_tokens
                    )
                ):
                    raise ValueError("settled usage cannot be changed")
                return
            if row["status"] != "reserved":
                raise ValueError("only a live reservation can be settled")
            connection.execute(
                "UPDATE budget SET actual = ?, status = 'settled' WHERE operation_id = ?",
                (actual, operation_id),
            )
            if attempt is not None:
                prompt_tokens, completion_tokens, total_tokens = usage or (None, None, None)
                connection.execute(
                    """UPDATE model_attempts
                    SET status = 'settled',
                        prompt_tokens = ?, completion_tokens = ?, total_tokens = ?,
                        cached_prompt_tokens = ?, reasoning_tokens = ?,
                        updated_at = ?
                    WHERE operation_id = ?""",
                    (
                        prompt_tokens,
                        completion_tokens,
                        total_tokens,
                        cached_prompt_tokens,
                        reasoning_tokens,
                        timestamp(now),
                        operation_id,
                    ),
                )
            overrun = actual > row["reserved"]
        if overrun:
            # Raise after COMMIT so unexpectedly large real costs never disappear.
            raise BudgetOverrunError("actual charge recorded; reservation was too small")

    def cancel_before_call(self, operation_id: str) -> None:
        """Release only when no external call started; unknown usage stays reserved."""
        with self.database.transaction() as connection:
            attempt = connection.execute(
                "SELECT status FROM model_attempts WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if attempt is not None and attempt["status"] != "reserved":
                raise ValueError("only an unstarted attempt can be cancelled")
            changed = connection.execute(
                """UPDATE budget SET status = 'cancelled'
                WHERE operation_id = ? AND status = 'reserved'""",
                (operation_id,),
            ).rowcount
            if changed != 1:
                raise ValueError("no live reservation to cancel")
            connection.execute(
                """UPDATE model_attempts
                SET status = 'cancelled', outcome_code = 'cancelled_before_call',
                    updated_at = ?
                WHERE operation_id = ? AND status = 'reserved'""",
                (timestamp(datetime.now(UTC)), operation_id),
            )

    def record_model_outcome(self, operation_id: str, outcome_code: str) -> None:
        """Record one safe result code without releasing unknown usage or replacing evidence."""
        if fullmatch(r"[a-z][a-z0-9_]{0,63}", outcome_code) is None:
            raise ValueError("outcome_code must be a short machine-readable code")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status, outcome_code FROM model_attempts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            if row["status"] == "cancelled":
                raise ValueError("a cancelled attempt cannot have a call outcome")
            if row["outcome_code"] is not None:
                if row["outcome_code"] == outcome_code:
                    return
                raise ValueError("a recorded model outcome cannot be changed")
            if outcome_code == "accepted" and row["status"] != "settled":
                raise ValueError("accepted requires settled usage")
            connection.execute(
                """UPDATE model_attempts
                SET status = CASE WHEN status = 'settled' THEN 'settled' ELSE 'unknown' END,
                    outcome_code = ?, updated_at = ?
                WHERE operation_id = ?""",
                (outcome_code, timestamp(datetime.now(UTC)), operation_id),
            )

    def recover_interrupted_model_attempts(self) -> int:
        """Only the runtime holding the data-directory lock may call this at startup."""
        with self.database.transaction() as connection:
            return connection.execute(
                """UPDATE model_attempts
                SET status = CASE WHEN status = 'reserved' THEN 'unknown' ELSE status END,
                    outcome_code = 'interrupted', updated_at = ?
                WHERE status IN ('reserved', 'settled') AND outcome_code IS NULL""",
                (timestamp(datetime.now(UTC)),),
            ).rowcount

    def model_attempt(self, operation_id: str) -> ModelAttemptRecord:
        row = self.database.connection.execute(
            """SELECT model_attempts.*, budget.status AS budget_status,
            budget.reserved, budget.actual
            FROM model_attempts JOIN budget USING (operation_id)
            WHERE model_attempts.operation_id = ?""",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return _model_attempt_record(row)

    def list_model_attempts(self, *, trace_id: str | None = None) -> tuple[ModelAttemptRecord, ...]:
        if trace_id is not None and not trace_id:
            raise ValueError("trace_id must be nonempty when provided")
        query = """SELECT model_attempts.*, budget.status AS budget_status,
        budget.reserved, budget.actual
        FROM model_attempts JOIN budget USING (operation_id)"""
        params: tuple[str, ...] = ()
        if trace_id is not None:
            query += " WHERE model_attempts.trace_id = ?"
            params = (trace_id,)
        query += " ORDER BY model_attempts.created_at, model_attempts.operation_id"
        rows = self.database.connection.execute(query, params).fetchall()
        return tuple(_model_attempt_record(row) for row in rows)


def _validate_usage(
    usage: tuple[int, int, int], cached_prompt_tokens: int | None, reasoning_tokens: int | None
) -> None:
    if len(usage) != 3 or any(type(value) is not int for value in usage):
        raise ValueError("usage must contain integer prompt, completion and total tokens")
    prompt_tokens, completion_tokens, total_tokens = usage
    if prompt_tokens < 1 or completion_tokens < 0 or total_tokens < 1:
        raise ValueError("usage token counts are outside the permitted range")
    if total_tokens != prompt_tokens + completion_tokens:
        raise ValueError("usage totals are inconsistent")
    for detail, total in (
        (cached_prompt_tokens, prompt_tokens),
        (reasoning_tokens, completion_tokens),
    ):
        if detail is not None and (type(detail) is not int or not 0 <= detail <= total):
            raise ValueError("token details must be integers within the corresponding total")


def _model_attempt_record(row) -> ModelAttemptRecord:
    return ModelAttemptRecord(
        operation_id=row["operation_id"],
        trace_id=row["trace_id"],
        budget_status=row["budget_status"],
        status=row["status"],
        reserved_microusd=row["reserved"],
        actual_microusd=row["actual"],
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        total_tokens=row["total_tokens"],
        cached_prompt_tokens=row["cached_prompt_tokens"],
        reasoning_tokens=row["reasoning_tokens"],
        outcome_code=row["outcome_code"],
        created_at=datetime.fromtimestamp(row["created_at"], UTC),
        updated_at=datetime.fromtimestamp(row["updated_at"], UTC),
    )
