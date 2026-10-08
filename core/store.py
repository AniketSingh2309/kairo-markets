"""Local persistence (SQLite, stdlib) for portfolio transactions and price alerts.

Single-user, local-first: the database lives in ``data/kairo.db`` (configurable via
KAIRO_DB_PATH) and never leaves this machine. All access goes through ``Store``,
which serialises writes with a lock so FastAPI's worker threads can share it.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    side        TEXT    NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity    REAL    NOT NULL CHECK (quantity > 0),
    price       REAL    NOT NULL CHECK (price > 0),
    fees        REAL    NOT NULL DEFAULT 0 CHECK (fees >= 0),
    trade_date  TEXT    NOT NULL,
    currency    TEXT,
    note        TEXT    NOT NULL DEFAULT '',
    source      TEXT    NOT NULL DEFAULT 'manual',
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_txn_symbol ON transactions(symbol);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    kind            TEXT    NOT NULL,
    threshold       REAL    NOT NULL,
    note            TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'triggered')),
    created_at      TEXT    NOT NULL,
    triggered_at    TEXT,
    triggered_price REAL,
    triggered_value REAL
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty           REAL    NOT NULL CHECK (qty > 0),
    order_type    TEXT    NOT NULL,
    price         REAL,
    trigger_price REAL,
    product       TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    session       TEXT    NOT NULL,
    fill_price    REAL,
    message       TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_fills (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id  INTEGER REFERENCES paper_orders(id) ON DELETE CASCADE,
    at        TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    side      TEXT    NOT NULL,
    qty       REAL    NOT NULL,
    price     REAL    NOT NULL,
    product   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS oi_snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL,
    expiry    TEXT NOT NULL,
    at        TEXT NOT NULL,
    spot      REAL,
    ce_oi     REAL,
    pe_oi     REAL,
    pcr       REAL,
    max_pain  REAL
);
CREATE INDEX IF NOT EXISTS ix_oi_snap ON oi_snapshots(symbol, expiry, at);
CREATE TABLE IF NOT EXISTS paper_gtt (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('single', 'oco')),
    side          TEXT NOT NULL,
    qty           REAL NOT NULL,
    product       TEXT NOT NULL,
    trigger       REAL NOT NULL,
    limit_price   REAL,
    stop_trigger  REAL,
    stop_limit    REAL,
    direction     TEXT,
    status        TEXT NOT NULL DEFAULT 'active',
    order_id      INTEGER,
    message       TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS baskets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    orders      TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # One shared connection: works for ":memory:" and is safe behind the lock.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # -- transactions -------------------------------------------------------

    def add_transactions(self, rows: list[dict[str, Any]], source: str = "manual") -> list[int]:
        ids = []
        with self._tx() as c:
            for r in rows:
                cur = c.execute(
                    "INSERT INTO transactions (symbol, side, quantity, price, fees, trade_date, currency, note,"
                    " source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (r["symbol"], r["side"], r["quantity"], r["price"], r.get("fees", 0.0),
                     str(r["trade_date"]), r.get("currency"), r.get("note", ""), source, _now()),
                )
                ids.append(int(cur.lastrowid))
        return ids

    def list_transactions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM transactions ORDER BY trade_date, id").fetchall()
        return [dict(r) for r in rows]

    def delete_transaction(self, txn_id: int) -> bool:
        with self._tx() as c:
            return c.execute("DELETE FROM transactions WHERE id = ?", (txn_id,)).rowcount > 0

    def clear_transactions(self) -> int:
        with self._tx() as c:
            return c.execute("DELETE FROM transactions").rowcount

    # -- alerts ---------------------------------------------------------------

    def add_alert(self, symbol: str, kind: str, threshold: float, note: str = "") -> int:
        with self._tx() as c:
            cur = c.execute("INSERT INTO alerts (symbol, kind, threshold, note, created_at) VALUES (?,?,?,?,?)",
                            (symbol, kind, threshold, note, _now()))
            return int(cur.lastrowid)

    def list_alerts(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._conn.execute("SELECT * FROM alerts WHERE status = ? ORDER BY id", (status,)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM alerts ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def get_alert(self, alert_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        return dict(row) if row else None

    def delete_alert(self, alert_id: int) -> bool:
        with self._tx() as c:
            return c.execute("DELETE FROM alerts WHERE id = ?", (alert_id,)).rowcount > 0

    def trigger_alert(self, alert_id: int, price: float, value: float, at: dt.datetime) -> bool:
        """Mark an active alert as triggered. Returns False if it was already triggered (idempotent)."""
        with self._tx() as c:
            return c.execute(
                "UPDATE alerts SET status='triggered', triggered_at=?, triggered_price=?, triggered_value=?"
                " WHERE id=? AND status='active'", (at.isoformat(), price, value, alert_id)).rowcount > 0

    def rearm_alert(self, alert_id: int) -> bool:
        with self._tx() as c:
            return c.execute(
                "UPDATE alerts SET status='active', triggered_at=NULL, triggered_price=NULL, triggered_value=NULL"
                " WHERE id=?", (alert_id,)).rowcount > 0

    # -- paper trading --------------------------------------------------------

    def add_paper_order(self, row: dict[str, Any]) -> int:
        now = _now()
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO paper_orders (symbol, side, qty, order_type, price, trigger_price, product, status, session,"
                " message, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["symbol"], row["side"], row["qty"], row["order_type"], row.get("price"), row.get("trigger_price"),
                 row["product"], row["status"], row["session"], row.get("message", ""), row.get("created_at", now), now))
            return int(cur.lastrowid)

    def paper_orders(self, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if statuses:
                marks = ",".join("?" * len(statuses))
                rows = self._conn.execute(f"SELECT * FROM paper_orders WHERE status IN ({marks}) ORDER BY id", statuses).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM paper_orders ORDER BY id DESC LIMIT 500").fetchall()
        return [dict(r) for r in rows]

    def update_paper_order(self, order_id: int, *, expect: tuple[str, ...], **fields: Any) -> bool:
        """Change an order only if it's still in one of the expected states (so two updaters can't both win)."""
        cols = ", ".join(f"{k}=?" for k in fields)
        marks = ",".join("?" * len(expect))
        with self._tx() as c:
            return c.execute(f"UPDATE paper_orders SET {cols}, updated_at=? WHERE id=? AND status IN ({marks})",
                             (*fields.values(), _now(), order_id, *expect)).rowcount > 0

    def fill_paper_order(self, order_id: int, expect: tuple[str, ...], fill: dict[str, Any], message: str = "") -> bool:
        """Mark complete and record the fill in one transaction (or neither, if the order moved on)."""
        marks = ",".join("?" * len(expect))
        with self._tx() as c:
            ok = c.execute(f"UPDATE paper_orders SET status='complete', fill_price=?, message=COALESCE(NULLIF(?, ''), message), updated_at=? "
                           f"WHERE id=? AND status IN ({marks})", (fill["price"], message, _now(), order_id, *expect)).rowcount > 0
            if ok:
                c.execute("INSERT INTO paper_fills (order_id, at, symbol, side, qty, price, product) VALUES (?,?,?,?,?,?,?)",
                          (order_id, fill["at"], fill["symbol"], fill["side"], fill["qty"], fill["price"], fill["product"]))
            return ok

    def paper_fills(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM paper_fills ORDER BY at, id").fetchall()
        return [dict(r) for r in rows]

    def reset_paper(self) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM paper_fills")
            c.execute("DELETE FROM paper_orders")
            c.execute("DELETE FROM paper_gtt")

    # -- settings -------------------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str | None) -> None:
        with self._tx() as c:
            if value is None:
                c.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                c.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                          (key, value))

    # -- option-chain snapshots (PCR / max pain through the day) ------------------

    def add_oi_snapshot(self, row: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO oi_snapshots (symbol, expiry, at, spot, ce_oi, pe_oi, pcr, max_pain) VALUES (?,?,?,?,?,?,?,?)",
                      (row["symbol"], row["expiry"], row["at"], row.get("spot"), row.get("ce_oi"), row.get("pe_oi"),
                       row.get("pcr"), row.get("max_pain")))
            c.execute("DELETE FROM oi_snapshots WHERE at < ?", ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).isoformat(),))

    def oi_snapshots(self, symbol: str, expiry: str, since: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT at, spot, ce_oi, pe_oi, pcr, max_pain FROM oi_snapshots WHERE symbol=? AND expiry=? AND at>=? ORDER BY at",
                                      (symbol, expiry, since)).fetchall()
        return [dict(r) for r in rows]

    # -- paper GTT / OCO --------------------------------------------------------------

    def add_gtt(self, row: dict[str, Any]) -> int:
        now = _now()
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO paper_gtt (symbol, kind, side, qty, product, trigger, limit_price, stop_trigger, stop_limit, direction,"
                " status, created_at, expires_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["symbol"], row["kind"], row["side"], row["qty"], row["product"], row["trigger"], row.get("limit"),
                 row.get("stop_trigger"), row.get("stop_limit"), row.get("direction"), "active", now, row["expires_at"], now))
            return int(cur.lastrowid)

    def gtts(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._conn.execute("SELECT * FROM paper_gtt WHERE status = ? ORDER BY id", (status,)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM paper_gtt ORDER BY id DESC LIMIT 200").fetchall()
        return [dict(r) for r in rows]

    def update_gtt(self, gtt_id: int, *, expect: str = "active", **fields: Any) -> bool:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._tx() as c:
            return c.execute(f"UPDATE paper_gtt SET {cols}, updated_at=? WHERE id=? AND status=?",
                             (*fields.values(), _now(), gtt_id, expect)).rowcount > 0

    # -- baskets --------------------------------------------------------------------

    def baskets(self) -> list[dict[str, Any]]:
        import json as _json
        with self._lock:
            rows = self._conn.execute("SELECT * FROM baskets ORDER BY id DESC").fetchall()
        return [{**dict(r), "orders": _json.loads(r["orders"])} for r in rows]

    def save_basket(self, name: str, orders: list[dict[str, Any]], basket_id: int | None = None) -> int:
        import json as _json
        now = _now()
        with self._tx() as c:
            if basket_id is None:
                cur = c.execute("INSERT INTO baskets (name, orders, created_at, updated_at) VALUES (?,?,?,?)",
                                (name, _json.dumps(orders), now, now))
                return int(cur.lastrowid)
            c.execute("UPDATE baskets SET name=?, orders=?, updated_at=? WHERE id=?", (name, _json.dumps(orders), now, basket_id))
            return basket_id

    def delete_basket(self, basket_id: int) -> bool:
        with self._tx() as c:
            return c.execute("DELETE FROM baskets WHERE id = ?", (basket_id,)).rowcount > 0
