from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Iterator

from config.settings import ExecutionMode
from domain.enums import (
    AccountRole,
    AssetClass,
    Currency,
    Market,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from domain.money import Money
from domain.order import Fill, OrderIntent, OrderResult
from domain.portfolio import NavSnapshot
from domain.position import CashSnapshot, Position


# --- 내부 직렬화 helper (public API에 노출하지 않음) ---


def _decimal_to_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _str_to_decimal(value: str | None, *, field_name: str) -> Decimal | None:
    if value is None:
        return None
    from domain._decimal import to_decimal

    return to_decimal(value, field_name=field_name)


def _datetime_to_str(value: datetime) -> str:
    return value.isoformat()


def _str_to_datetime(value: str, *, field_name: str) -> datetime:
    from domain._datetime import require_timezone_aware_datetime

    parsed = datetime.fromisoformat(value)
    return require_timezone_aware_datetime(parsed, field_name=field_name)


def _enum_to_str(value: StrEnum) -> str:
    return value.value


def _str_to_enum(enum_cls: type[StrEnum], value: str) -> StrEnum:
    return enum_cls(value)


@dataclass(frozen=True)
class CashLedgerEntry:
    """append-only paper_cash_ledger row를 표현한다."""

    id: int | None
    order_id: str | None
    correlation_id: str | None
    currency: Currency
    account_role: AccountRole
    delta_amount: Decimal
    balance_after: Decimal
    reason: str
    created_at: datetime


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS order_intents (
    order_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    account_role TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    execution_mode TEXT NOT NULL,
    time_in_force TEXT NOT NULL,
    quantity TEXT,
    target_weight_percent TEXT,
    limit_price TEXT,
    reason_code TEXT,
    source_decision_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_results (
    order_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    rejection_reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES order_intents(order_id)
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity TEXT NOT NULL,
    fill_price TEXT NOT NULL,
    commission_amount TEXT NOT NULL,
    commission_currency TEXT NOT NULL,
    tax_amount TEXT NOT NULL,
    tax_currency TEXT NOT NULL,
    slippage_amount TEXT,
    slippage_currency TEXT,
    filled_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES order_intents(order_id)
);

CREATE TABLE IF NOT EXISTS current_cash (
    currency TEXT NOT NULL,
    account_role TEXT NOT NULL,
    amount TEXT NOT NULL,
    as_of TEXT NOT NULL,
    PRIMARY KEY (currency, account_role)
);

CREATE TABLE IF NOT EXISTS paper_cash_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT,
    correlation_id TEXT,
    currency TEXT NOT NULL,
    account_role TEXT NOT NULL,
    delta_amount TEXT NOT NULL,
    balance_after TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS current_positions (
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,
    account_role TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    quantity TEXT NOT NULL,
    avg_cost TEXT NOT NULL,
    currency TEXT NOT NULL,
    market_price TEXT,
    PRIMARY KEY (symbol, market, account_role)
);

CREATE TABLE IF NOT EXISTS nav_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    total_nav_krw TEXT NOT NULL,
    cash_krw TEXT NOT NULL,
    invested_krw TEXT NOT NULL,
    daily_return_percent TEXT,
    mdd_percent TEXT
);
"""


class SQLiteLedger:
    """장기 paper trading 원장. sqlite3 표준 라이브러리만 사용한다."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def close(self) -> None:
        """SQLite 연결을 닫는다."""
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """원장 쓰기를 transaction 단위로 처리한다."""
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def has_processed_order(self, order_id: str) -> bool:
        """order_result가 이미 기록된 order_id인지 확인한다."""
        row = self._conn.execute(
            "SELECT 1 FROM order_results WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        return row is not None

    def save_order_intent(self, intent: OrderIntent) -> None:
        """주문 의도를 원장에 기록한다."""
        self._conn.execute(
            """
            INSERT INTO order_intents (
                order_id, correlation_id, symbol, market, asset_class,
                account_role, side, order_type, execution_mode, time_in_force,
                quantity, target_weight_percent, limit_price, reason_code,
                source_decision_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent.order_id,
                intent.correlation_id,
                intent.symbol,
                _enum_to_str(intent.market),
                _enum_to_str(intent.asset_class),
                _enum_to_str(intent.account_role),
                _enum_to_str(intent.side),
                _enum_to_str(intent.order_type),
                _enum_to_str(intent.execution_mode),
                _enum_to_str(intent.time_in_force),
                _decimal_to_str(intent.quantity),
                _decimal_to_str(intent.target_weight_percent),
                _decimal_to_str(intent.limit_price),
                intent.reason_code,
                intent.source_decision_id,
                _datetime_to_str(intent.created_at),
            ),
        )

    def save_order_result(self, result: OrderResult) -> None:
        """주문 결과를 원장에 기록한다."""
        self._conn.execute(
            """
            INSERT INTO order_results (order_id, status, accepted, rejection_reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                result.order_id,
                _enum_to_str(result.status),
                1 if result.accepted else 0,
                result.rejection_reason,
                _datetime_to_str(result.created_at),
            ),
        )

    def save_fill(self, fill: Fill) -> None:
        """체결을 원장에 기록한다."""
        slippage_amount: str | None = None
        slippage_currency: str | None = None
        if fill.slippage is not None:
            slippage_amount = _decimal_to_str(fill.slippage.amount)
            slippage_currency = _enum_to_str(fill.slippage.currency)

        self._conn.execute(
            """
            INSERT INTO fills (
                fill_id, order_id, symbol, market, side, quantity, fill_price,
                commission_amount, commission_currency, tax_amount, tax_currency,
                slippage_amount, slippage_currency, filled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.fill_id,
                fill.order_id,
                fill.symbol,
                _enum_to_str(fill.market),
                _enum_to_str(fill.side),
                _decimal_to_str(fill.quantity),
                _decimal_to_str(fill.fill_price),
                _decimal_to_str(fill.commission.amount),
                _enum_to_str(fill.commission.currency),
                _decimal_to_str(fill.tax.amount),
                _enum_to_str(fill.tax.currency),
                slippage_amount,
                slippage_currency,
                _datetime_to_str(fill.filled_at),
            ),
        )

    def get_order_intent(self, order_id: str) -> OrderIntent | None:
        """주문 의도를 domain model로 복원한다."""
        row = self._conn.execute(
            "SELECT * FROM order_intents WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_order_intent(row)

    def get_order_result(self, order_id: str) -> OrderResult | None:
        """주문 결과를 domain model로 복원한다."""
        row = self._conn.execute(
            "SELECT * FROM order_results WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_order_result(row)

    def get_fill_by_order_id(self, order_id: str) -> Fill | None:
        """order_id에 연결된 체결을 domain model로 복원한다."""
        row = self._conn.execute(
            "SELECT * FROM fills WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_fill(row)

    def _upsert_cash(self, cash: CashSnapshot) -> None:
        """현재 현금 projection을 갱신한다. public API가 아니다."""
        self._conn.execute(
            """
            INSERT INTO current_cash (currency, account_role, amount, as_of)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(currency, account_role) DO UPDATE SET
                amount = excluded.amount,
                as_of = excluded.as_of
            """,
            (
                _enum_to_str(cash.currency),
                _enum_to_str(cash.account_role),
                _decimal_to_str(cash.amount),
                _datetime_to_str(cash.as_of),
            ),
        )

    def _append_cash_ledger_entry(
        self,
        *,
        order_id: str | None,
        correlation_id: str | None,
        currency: Currency,
        account_role: AccountRole,
        delta_amount: Decimal,
        balance_after: Decimal,
        reason: str,
        created_at: datetime,
    ) -> None:
        """append-only paper_cash_ledger row를 추가한다. public API가 아니다."""
        self._conn.execute(
            """
            INSERT INTO paper_cash_ledger (
                order_id, correlation_id, currency, account_role,
                delta_amount, balance_after, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                correlation_id,
                _enum_to_str(currency),
                _enum_to_str(account_role),
                _decimal_to_str(delta_amount),
                _decimal_to_str(balance_after),
                reason,
                _datetime_to_str(created_at),
            ),
        )

    def apply_cash_change(
        self,
        cash: CashSnapshot,
        *,
        order_id: str | None,
        correlation_id: str | None,
        delta_amount: Decimal,
        reason: str,
    ) -> None:
        """current_cash projection과 paper_cash_ledger append를 함께 수행한다."""
        existing = self.get_cash(cash.currency, cash.account_role)
        previous_amount = existing.amount if existing is not None else Decimal("0")
        expected_balance = previous_amount + delta_amount
        if expected_balance != cash.amount:
            raise ValueError(
                "cash ledger balance mismatch: delta_amount does not match balance_after"
            )

        self._upsert_cash(cash)
        self._append_cash_ledger_entry(
            order_id=order_id,
            correlation_id=correlation_id,
            currency=cash.currency,
            account_role=cash.account_role,
            delta_amount=delta_amount,
            balance_after=cash.amount,
            reason=reason,
            created_at=cash.as_of,
        )

    def get_cash(self, currency: Currency, account_role: AccountRole) -> CashSnapshot | None:
        """현재 현금을 domain model로 복원한다."""
        row = self._conn.execute(
            """
            SELECT currency, account_role, amount, as_of
            FROM current_cash
            WHERE currency = ? AND account_role = ?
            """,
            (_enum_to_str(currency), _enum_to_str(account_role)),
        ).fetchone()
        if row is None:
            return None
        return _row_to_cash_snapshot(row)

    def list_cash_ledger_entries(
        self,
        *,
        currency: Currency | None = None,
        account_role: AccountRole | None = None,
    ) -> tuple[CashLedgerEntry, ...]:
        """paper_cash_ledger entries를 조회한다."""
        query = """
            SELECT id, order_id, correlation_id, currency, account_role,
                   delta_amount, balance_after, reason, created_at
            FROM paper_cash_ledger
        """
        params: list[str] = []
        filters: list[str] = []

        if currency is not None:
            filters.append("currency = ?")
            params.append(_enum_to_str(currency))
        if account_role is not None:
            filters.append("account_role = ?")
            params.append(_enum_to_str(account_role))

        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY id"

        rows = self._conn.execute(query, params).fetchall()
        return tuple(_row_to_cash_ledger_entry(row) for row in rows)

    def upsert_position(self, position: Position) -> None:
        """현재 포지션 상태를 갱신한다."""
        self._conn.execute(
            """
            INSERT INTO current_positions (
                symbol, market, account_role, asset_class,
                quantity, avg_cost, currency, market_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, market, account_role) DO UPDATE SET
                asset_class = excluded.asset_class,
                quantity = excluded.quantity,
                avg_cost = excluded.avg_cost,
                currency = excluded.currency,
                market_price = excluded.market_price
            """,
            (
                position.symbol,
                _enum_to_str(position.market),
                _enum_to_str(position.account_role),
                _enum_to_str(position.asset_class),
                _decimal_to_str(position.quantity),
                _decimal_to_str(position.avg_cost),
                _enum_to_str(position.currency),
                _decimal_to_str(position.market_price),
            ),
        )

    def delete_position(self, symbol: str, market: Market, account_role: AccountRole) -> None:
        """포지션 행을 삭제한다. 전량 매도 후 정리에 사용한다."""
        self._conn.execute(
            """
            DELETE FROM current_positions
            WHERE symbol = ? AND market = ? AND account_role = ?
            """,
            (symbol, _enum_to_str(market), _enum_to_str(account_role)),
        )

    def get_position(
        self,
        symbol: str,
        market: Market,
        account_role: AccountRole,
    ) -> Position | None:
        """포지션을 domain model로 복원한다."""
        row = self._conn.execute(
            """
            SELECT symbol, market, account_role, asset_class,
                   quantity, avg_cost, currency, market_price
            FROM current_positions
            WHERE symbol = ? AND market = ? AND account_role = ?
            """,
            (symbol, _enum_to_str(market), _enum_to_str(account_role)),
        ).fetchone()
        if row is None:
            return None
        return _row_to_position(row)

    def list_positions(self) -> tuple[Position, ...]:
        """보유 포지션 전체를 domain model로 복원한다."""
        rows = self._conn.execute(
            """
            SELECT symbol, market, account_role, asset_class,
                   quantity, avg_cost, currency, market_price
            FROM current_positions
            ORDER BY symbol, market, account_role
            """
        ).fetchall()
        return tuple(_row_to_position(row) for row in rows)

    def save_nav_snapshot(self, snapshot: NavSnapshot) -> None:
        """NAV 스냅샷을 저장한다. 자동 계산은 하지 않는다."""
        self._conn.execute(
            """
            INSERT INTO nav_snapshots (
                snapshot_id, as_of, total_nav_krw, cash_krw, invested_krw,
                daily_return_percent, mdd_percent
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_id,
                _datetime_to_str(snapshot.as_of),
                _decimal_to_str(snapshot.total_nav_krw),
                _decimal_to_str(snapshot.cash_krw),
                _decimal_to_str(snapshot.invested_krw),
                _decimal_to_str(snapshot.daily_return_percent),
                _decimal_to_str(snapshot.mdd_percent),
            ),
        )

    def list_nav_snapshots(self) -> tuple[NavSnapshot, ...]:
        """저장된 NAV 스냅샷 목록을 as_of, snapshot_id 순으로 반환한다."""
        rows = self._conn.execute(
            """
            SELECT snapshot_id, as_of, total_nav_krw, cash_krw, invested_krw,
                   daily_return_percent, mdd_percent
            FROM nav_snapshots
            ORDER BY as_of ASC, snapshot_id ASC
            """
        ).fetchall()
        return tuple(_row_to_nav_snapshot(row) for row in rows)

    def list_tables(self) -> tuple[str, ...]:
        """스키마 초기화 검증용. 생성된 user table 이름을 반환한다."""
        rows = self._conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        return tuple(row["name"] for row in rows)

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()


def _row_to_order_intent(row: sqlite3.Row) -> OrderIntent:
    return OrderIntent(
        order_id=row["order_id"],
        correlation_id=row["correlation_id"],
        symbol=row["symbol"],
        market=_str_to_enum(Market, row["market"]),
        asset_class=_str_to_enum(AssetClass, row["asset_class"]),
        account_role=_str_to_enum(AccountRole, row["account_role"]),
        side=_str_to_enum(OrderSide, row["side"]),
        order_type=_str_to_enum(OrderType, row["order_type"]),
        execution_mode=_str_to_enum(ExecutionMode, row["execution_mode"]),
        time_in_force=_str_to_enum(TimeInForce, row["time_in_force"]),
        quantity=_str_to_decimal(row["quantity"], field_name="quantity"),
        target_weight_percent=_str_to_decimal(row["target_weight_percent"], field_name="target_weight_percent"),
        limit_price=_str_to_decimal(row["limit_price"], field_name="limit_price"),
        reason_code=row["reason_code"],
        source_decision_id=row["source_decision_id"],
        created_at=_str_to_datetime(row["created_at"], field_name="created_at"),
    )


def _row_to_order_result(row: sqlite3.Row) -> OrderResult:
    return OrderResult(
        order_id=row["order_id"],
        status=_str_to_enum(OrderStatus, row["status"]),
        accepted=bool(row["accepted"]),
        rejection_reason=row["rejection_reason"],
        created_at=_str_to_datetime(row["created_at"], field_name="created_at"),
    )


def _row_to_fill(row: sqlite3.Row) -> Fill:
    slippage: Money | None = None
    if row["slippage_amount"] is not None:
        slippage = Money(
            amount=_str_to_decimal(row["slippage_amount"], field_name="slippage_amount"),
            currency=_str_to_enum(Currency, row["slippage_currency"]),
        )

    return Fill(
        fill_id=row["fill_id"],
        order_id=row["order_id"],
        symbol=row["symbol"],
        market=_str_to_enum(Market, row["market"]),
        side=_str_to_enum(OrderSide, row["side"]),
        quantity=_str_to_decimal(row["quantity"], field_name="quantity"),
        fill_price=_str_to_decimal(row["fill_price"], field_name="fill_price"),
        commission=Money(
            amount=_str_to_decimal(row["commission_amount"], field_name="commission_amount"),
            currency=_str_to_enum(Currency, row["commission_currency"]),
        ),
        tax=Money(
            amount=_str_to_decimal(row["tax_amount"], field_name="tax_amount"),
            currency=_str_to_enum(Currency, row["tax_currency"]),
        ),
        slippage=slippage,
        filled_at=_str_to_datetime(row["filled_at"], field_name="filled_at"),
    )


def _row_to_cash_snapshot(row: sqlite3.Row) -> CashSnapshot:
    return CashSnapshot(
        currency=_str_to_enum(Currency, row["currency"]),
        amount=_str_to_decimal(row["amount"], field_name="amount"),
        account_role=_str_to_enum(AccountRole, row["account_role"]),
        as_of=_str_to_datetime(row["as_of"], field_name="as_of"),
    )


def _row_to_cash_ledger_entry(row: sqlite3.Row) -> CashLedgerEntry:
    return CashLedgerEntry(
        id=row["id"],
        order_id=row["order_id"],
        correlation_id=row["correlation_id"],
        currency=_str_to_enum(Currency, row["currency"]),
        account_role=_str_to_enum(AccountRole, row["account_role"]),
        delta_amount=_str_to_decimal(row["delta_amount"], field_name="delta_amount"),
        balance_after=_str_to_decimal(row["balance_after"], field_name="balance_after"),
        reason=row["reason"],
        created_at=_str_to_datetime(row["created_at"], field_name="created_at"),
    )


def _row_to_position(row: sqlite3.Row) -> Position:
    return Position(
        symbol=row["symbol"],
        market=_str_to_enum(Market, row["market"]),
        asset_class=_str_to_enum(AssetClass, row["asset_class"]),
        account_role=_str_to_enum(AccountRole, row["account_role"]),
        quantity=_str_to_decimal(row["quantity"], field_name="quantity"),
        avg_cost=_str_to_decimal(row["avg_cost"], field_name="avg_cost"),
        currency=_str_to_enum(Currency, row["currency"]),
        market_price=_str_to_decimal(row["market_price"], field_name="market_price"),
    )


def _row_to_nav_snapshot(row: sqlite3.Row) -> NavSnapshot:
    return NavSnapshot(
        snapshot_id=row["snapshot_id"],
        as_of=_str_to_datetime(row["as_of"], field_name="as_of"),
        total_nav_krw=_str_to_decimal(row["total_nav_krw"], field_name="total_nav_krw"),
        cash_krw=_str_to_decimal(row["cash_krw"], field_name="cash_krw"),
        invested_krw=_str_to_decimal(row["invested_krw"], field_name="invested_krw"),
        daily_return_percent=_str_to_decimal(
            row["daily_return_percent"],
            field_name="daily_return_percent",
        ),
        mdd_percent=_str_to_decimal(row["mdd_percent"], field_name="mdd_percent"),
    )
