"""Read-only connector for the old IIC booking MySQL database.

Credentials come only from Django settings / environment variables.
This module never logs passwords or writes to the old database.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Iterator

from django.conf import settings

logger = logging.getLogger(__name__)

WRITE_GRANT_TOKENS = ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "TRUNCATE", "CREATE")
READONLY_SQL_PREFIXES = ("SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "WITH")


def assert_readonly_sql(sql: str) -> None:
    """Refuse mutating SQL at the reader boundary (defense in depth)."""
    text = (sql or "").strip().lstrip("(").strip()
    if not text:
        raise ValueError("Empty SQL refused by OldMySQLReader")
    # Multi-statement: reject if a write verb appears after a semicolon.
    compact = " ".join(text.split()).upper()
    parts = [p.strip() for p in compact.split(";") if p.strip()]
    for part in parts:
        first = part.split(None, 1)[0].upper()
        if first not in READONLY_SQL_PREFIXES:
            raise ValueError(
                f"OldMySQLReader refused non-read SQL starting with {first!r}. "
                "REAL legacy integration is READ-ONLY "
                "(no INSERT/UPDATE/DELETE/ALTER/DROP/TRUNCATE)."
            )


REQUIRED_TABLES = ("users", "user_wallet", "wallet_transactions")
REQUIRED_COLUMNS = {
    "users": ("id", "emp_id", "email", "name"),
    "user_wallet": ("id", "user_id", "balance"),
    "wallet_transactions": (
        "id",
        "user_id",
        "amount",
        "balance",
        "transaction_type",
        "create_date",
        "description",
        "created_by",
    ),
}


class OldMySQLNotConfigured(Exception):
    """OLD_MYSQL_* / LEGACY_MYSQL_* not set."""


class OldMySQLConnectionError(Exception):
    """Could not connect or query."""


def _mysql_settings() -> dict[str, Any]:
    host = (getattr(settings, "OLD_MYSQL_HOST", None) or "").strip()
    user = (getattr(settings, "OLD_MYSQL_USER", None) or "").strip()
    password = getattr(settings, "OLD_MYSQL_PASSWORD", None) or ""
    database = (getattr(settings, "OLD_MYSQL_DATABASE", None) or "").strip()
    port = int(getattr(settings, "OLD_MYSQL_PORT", 3306) or 3306)
    if not host or not user or not database:
        raise OldMySQLNotConfigured(
            "Set OLD_MYSQL_HOST, OLD_MYSQL_USER, OLD_MYSQL_DATABASE "
            "(and OLD_MYSQL_PASSWORD) via environment or secrets. "
            "Do not pass credentials in API bodies or source code."
        )
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
        "connect_timeout": int(getattr(settings, "OLD_MYSQL_CONNECT_TIMEOUT", 15)),
        "read_timeout": int(getattr(settings, "OLD_MYSQL_READ_TIMEOUT", 60)),
        "charset": "utf8mb4",
        "cursorclass": None,
    }


class OldMySQLReader:
    """Paginated, retrying, read-only reader. Does not load the whole database."""

    def __init__(self):
        self._conn = None

    def connect(self):
        import pymysql
        from pymysql.cursors import DictCursor

        cfg = _mysql_settings()
        last_err = None
        for attempt in range(1, 3):
            try:
                self._conn = pymysql.connect(
                    host=cfg["host"],
                    port=cfg["port"],
                    user=cfg["user"],
                    password=cfg["password"],
                    database=cfg["database"],
                    connect_timeout=min(cfg["connect_timeout"], 10),
                    read_timeout=cfg["read_timeout"],
                    charset=cfg["charset"],
                    cursorclass=DictCursor,
                    autocommit=True,
                )
                return self._conn
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning("Old MySQL connect attempt %s failed: %s", attempt, type(exc).__name__)
                if attempt < 2:
                    time.sleep(1)
        raise OldMySQLConnectionError(
            f"Could not connect to old MySQL ({type(last_err).__name__}). "
            "Typical classes: timeout/routing/firewall if unreachable; "
            "authentication if Access denied; database selection if Unknown database."
        )

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()

    def _cursor(self):
        if self._conn is None:
            self.connect()
        return self._conn.cursor()

    def fetchone(self, sql: str, params: tuple = ()):
        assert_readonly_sql(sql)
        with self._cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def fetchall(self, sql: str, params: tuple = ()):
        assert_readonly_sql(sql)
        with self._cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def connection_probe(self) -> dict[str, Any]:
        """Non-mutating connectivity and schema checks. Never logs secrets."""
        cfg = _mysql_settings()
        row = self.fetchone(
            "SELECT DATABASE() AS db_name, USER() AS db_user, VERSION() AS version, "
            "@@character_set_database AS charset, @@collation_database AS collation, "
            "@@session.time_zone AS session_tz, @@global.time_zone AS global_tz, "
            "@@read_only AS read_only"
        )
        grants = self.fetchall("SHOW GRANTS")
        grant_text = " ".join(str(v) for g in grants for v in g.values()).upper()
        writable = "ALL PRIVILEGES" in grant_text or any(tok in grant_text for tok in WRITE_GRANT_TOKENS)
        tables = {next(iter(t.values())) for t in self.fetchall("SHOW TABLES")}
        missing_tables = [t for t in REQUIRED_TABLES if t not in tables]
        missing_columns = {}
        for table, cols in REQUIRED_COLUMNS.items():
            if table in missing_tables:
                continue
            existing = {c["Field"] for c in self.fetchall(f"SHOW COLUMNS FROM `{table}`")}
            miss = [c for c in cols if c not in existing]
            if miss:
                missing_columns[table] = miss
        counts = {}
        for table in REQUIRED_TABLES:
            if table not in missing_tables:
                counts[table] = int(self.fetchone(f"SELECT COUNT(*) AS c FROM `{table}`")["c"])
        txn_range = None
        if "wallet_transactions" not in missing_tables:
            txn_range = self.fetchone(
                "SELECT MIN(id) AS min_id, MAX(id) AS max_id, "
                "MIN(create_date) AS min_date, MAX(create_date) AS max_date "
                "FROM wallet_transactions"
            )
        return {
            "ok": not missing_tables and not missing_columns,
            "database": row["db_name"] if row else None,
            "username_reported": row["db_user"] if row else None,
            "server_version": row["version"] if row else None,
            "charset": row["charset"] if row else None,
            "collation": row["collation"] if row else None,
            "session_timezone": row["session_tz"] if row else None,
            "global_timezone": row["global_tz"] if row else None,
            "mysql_read_only_flag": bool(row["read_only"]) if row else None,
            "account_appears_writable": writable,
            "writable_account_recommendation": (
                "The supplied MySQL account appears to have write privileges. "
                "Use a dedicated read-only migration user. Do not ALTER existing grants automatically."
                if writable
                else ""
            ),
            "configured_host": cfg["host"],
            "configured_port": cfg["port"],
            "configured_database": cfg["database"],
            "missing_tables": missing_tables,
            "missing_columns": missing_columns,
            "row_counts": counts,
            "wallet_transaction_id_range": txn_range,
            "schema_discovery": self.discover_schema(),
            "ssh_note": (
                "If campus cannot reach MySQL:3306, open an SSH tunnel on the migration host "
                "and set OLD_MYSQL_HOST/PORT to the tunnel endpoint. Django in Docker must not "
                "use 127.0.0.1 unless that is the container's own MySQL."
            ),
        }

    def discover_schema(self) -> dict:
        """READ-ONLY SHOW TABLES / SHOW COLUMNS. Classifies expected wallet identity fields."""
        tables = sorted(next(iter(t.values())) for t in self.fetchall("SHOW TABLES"))
        columns = {}
        indexes = {}
        for table in tables:
            columns[table] = [
                {"Field": c["Field"], "Type": c.get("Type"), "Key": c.get("Key"), "Extra": c.get("Extra")}
                for c in self.fetchall(f"SHOW COLUMNS FROM `{table}`")
            ]
            try:
                indexes[table] = [
                    {"Key_name": i.get("Key_name"), "Column_name": i.get("Column_name"), "Non_unique": i.get("Non_unique")}
                    for i in self.fetchall(f"SHOW INDEX FROM `{table}`")
                ]
            except Exception:
                indexes[table] = []
        col_names = {t: [c["Field"] for c in cols] for t, cols in columns.items()}
        def classify(table: str, expected_field: str) -> str:
            if table in col_names and expected_field in col_names[table]:
                return "VERIFIED"
            matches = [t for t, cols in col_names.items() if expected_field in cols]
            if not matches:
                return "NOT FOUND"
            return "AMBIGUOUS"

        mapping = {
            "user_table": {"assumed": "users", "status": "VERIFIED" if "users" in columns else "NOT FOUND"},
            "employee_id_field": {
                "assumed": "users.emp_id",
                "status": classify("users", "emp_id"),
            },
            "wallet_table": {"assumed": "user_wallet", "status": "VERIFIED" if "user_wallet" in col_names else "NOT FOUND"},
            "wallet_transaction_table": {
                "assumed": "wallet_transactions",
                "status": "VERIFIED" if "wallet_transactions" in col_names else "NOT FOUND",
            },
            "source_transaction_id": {
                "assumed": "wallet_transactions.id",
                "status": classify("wallet_transactions", "id"),
                "decision": "Use wallet_transactions.id as monotonic watermark and unique source_transaction_id.",
            },
            "transaction_date": {
                "assumed": "wallet_transactions.create_date",
                "status": classify("wallet_transactions", "create_date"),
            },
            "amount": {"assumed": "wallet_transactions.amount", "status": classify("wallet_transactions", "amount")},
            "transaction_type": {
                "assumed": "wallet_transactions.transaction_type (1=credit, 2=debit)",
                "status": classify("wallet_transactions", "transaction_type"),
            },
            "running_balance": {
                "assumed": "wallet_transactions.balance",
                "status": classify("wallet_transactions", "balance"),
                "note": "Comparison only. Ledger sum is financial truth.",
            },
            "description": {"assumed": "wallet_transactions.description", "status": classify("wallet_transactions", "description")},
            "utr_column": {
                "assumed": None,
                "status": "NOT FOUND" if "wallet_transactions" in col_names and "utr" not in col_names.get("wallet_transactions", []) else "VERIFIED",
                "note": "UTR parsed from description when present. Not fabricated.",
            },
            "booking_table": {"assumed": "booking", "status": "VERIFIED" if "booking" in col_names else "NOT FOUND"},
        }
        return {"tables": tables, "columns": columns, "indexes": indexes, "mapping": mapping}

    def iter_pk_range(self, table: str, pk: str, after_id: int, batch_size: int, where: str = "") -> Iterator[list[dict]]:
        sql_where = f" AND ({where})" if where else ""
        last = after_id
        while True:
            rows = self.fetchall(
                f"SELECT * FROM `{table}` WHERE `{pk}` > %s{sql_where} ORDER BY `{pk}` ASC LIMIT %s",
                (last, batch_size),
            )
            if not rows:
                return
            yield rows
            last = int(rows[-1][pk])

    def duplicate_employee_ids(self) -> set[str]:
        rows = self.fetchall(
            "SELECT TRIM(emp_id) AS eid, COUNT(*) AS c FROM users "
            "WHERE emp_id IS NOT NULL AND TRIM(emp_id) <> '' "
            "GROUP BY TRIM(emp_id) HAVING c > 1"
        )
        return {str(r["eid"]) for r in rows}

    def iter_users(self, batch_size: int = 500) -> Iterator[dict]:
        last = 0
        while True:
            rows = self.fetchall(
                "SELECT id, emp_id, name, email FROM users WHERE id > %s ORDER BY id ASC LIMIT %s",
                (last, batch_size),
            )
            if not rows:
                return
            yield from rows
            last = int(rows[-1]["id"])

    def wallet_for_user(self, user_id: int) -> dict | None:
        return self.fetchone(
            "SELECT id, user_id, balance FROM user_wallet WHERE user_id = %s LIMIT 1",
            (user_id,),
        )

    def wallets_by_user_id(self) -> dict[int, dict]:
        rows = self.fetchall("SELECT id, user_id, balance FROM user_wallet")
        return {int(r["user_id"]): r for r in rows}

    def user_ledger_totals(self, user_id: int) -> tuple[Decimal, Decimal]:
        row = self.fetchone(
            "SELECT "
            "COALESCE(SUM(CASE WHEN transaction_type = 1 THEN ABS(amount) ELSE 0 END), 0) AS credits, "
            "COALESCE(SUM(CASE WHEN transaction_type = 2 THEN ABS(amount) ELSE 0 END), 0) AS debits "
            "FROM wallet_transactions WHERE user_id = %s",
            (user_id,),
        )
        return Decimal(str(row["credits"])), Decimal(str(row["debits"]))

    def ledger_totals_by_user_id(self) -> dict[int, tuple[Decimal, Decimal]]:
        rows = self.fetchall(
            "SELECT user_id, "
            "COALESCE(SUM(CASE WHEN transaction_type = 1 THEN ABS(amount) ELSE 0 END), 0) AS credits, "
            "COALESCE(SUM(CASE WHEN transaction_type = 2 THEN ABS(amount) ELSE 0 END), 0) AS debits "
            "FROM wallet_transactions GROUP BY user_id"
        )
        return {
            int(r["user_id"]): (Decimal(str(r["credits"])), Decimal(str(r["debits"])))
            for r in rows
        }

    def transaction_stream_stats(self, after_id: int) -> dict:
        row = self.fetchone(
            "SELECT COUNT(*) AS n, MIN(id) AS min_id, MAX(id) AS max_id "
            "FROM wallet_transactions WHERE id > %s",
            (after_id,),
        )
        return {"n": int(row["n"] or 0), "min_id": row["min_id"], "max_id": row["max_id"]}

    def live_financial_audit(self) -> dict:
        types = self.fetchall(
            "SELECT transaction_type AS t, COUNT(*) AS n, "
            "COALESCE(SUM(ABS(amount)), 0) AS total "
            "FROM wallet_transactions GROUP BY transaction_type ORDER BY t"
        )
        credits = Decimal("0")
        debits = Decimal("0")
        credit_n = 0
        debit_n = 0
        other = []
        for r in types:
            t = r["t"]
            n = int(r["n"])
            total = Decimal(str(r["total"]))
            if int(t) == 1:
                credits = total
                credit_n = n
            elif int(t) == 2:
                debits = total
                debit_n = n
            else:
                other.append({"transaction_type": t, "count": n, "total": str(total)})
        uniq = self.fetchone(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT id) AS d FROM wallet_transactions"
        )
        idx = self.fetchall("SHOW INDEX FROM wallet_transactions")
        id_col = self.fetchone("SHOW COLUMNS FROM wallet_transactions LIKE 'id'")
        samples = self.fetchall(
            "SELECT transaction_type, description FROM wallet_transactions "
            "WHERE transaction_type IN (1, 2) ORDER BY id DESC LIMIT 8"
        )
        emp = self.fetchone(
            "SELECT COUNT(*) AS total_users, "
            "SUM(CASE WHEN emp_id IS NOT NULL AND TRIM(emp_id) <> '' THEN 1 ELSE 0 END) AS with_emp, "
            "SUM(CASE WHEN emp_id IS NULL OR TRIM(emp_id) = '' THEN 1 ELSE 0 END) AS without_emp "
            "FROM users"
        )
        dup = self.fetchone(
            "SELECT COUNT(*) AS duplicate_groups, COALESCE(SUM(c), 0) AS duplicate_rows FROM ("
            "SELECT TRIM(emp_id) AS eid, COUNT(*) AS c FROM users "
            "WHERE emp_id IS NOT NULL AND TRIM(emp_id) <> '' "
            "GROUP BY TRIM(emp_id) HAVING c > 1) d"
        )
        return {
            "users_total": int(emp["total_users"]),
            "users_with_employee_id": int(emp["with_emp"] or 0),
            "users_without_employee_id": int(emp["without_emp"] or 0),
            "duplicate_employee_id_groups": int(dup["duplicate_groups"] or 0),
            "duplicate_employee_id_rows": int(dup["duplicate_rows"] or 0),
            "wallet_count": int(self.fetchone("SELECT COUNT(*) AS c FROM user_wallet")["c"]),
            "transaction_count": int(uniq["n"]),
            "transaction_distinct_ids": int(uniq["d"]),
            "id_is_unique": int(uniq["n"]) == int(uniq["d"]),
            "id_column": id_col,
            "indexes": [
                {"key": i.get("Key_name"), "column": i.get("Column_name"), "unique": not i.get("Non_unique")}
                for i in idx
            ],
            "credit_count": credit_n,
            "debit_count": debit_n,
            "other_type_buckets": other,
            "total_credits": str(credits),
            "total_debits": str(debits),
            "calculated_closing_balance": str(credits - debits),
            "type1_negative_count": int(self.fetchone("SELECT COUNT(*) n FROM wallet_transactions WHERE transaction_type=1 AND amount<0")["n"]),
            "type2_negative_count": int(self.fetchone("SELECT COUNT(*) n FROM wallet_transactions WHERE transaction_type=2 AND amount<0")["n"]),
            "type2_zero_count": int(self.fetchone("SELECT COUNT(*) n FROM wallet_transactions WHERE transaction_type=2 AND amount=0")["n"]),
            "outlier_abs_gt_10m": int(self.fetchone("SELECT COUNT(*) n FROM wallet_transactions WHERE ABS(amount)>10000000")["n"]),
            "sum_user_wallet_balance_column": str(self.fetchone("SELECT COALESCE(SUM(balance),0) s FROM user_wallet")["s"]),
            "type_samples": [
                {"transaction_type": s["transaction_type"], "description_prefix": str(s.get("description") or "")[:80]}
                for s in samples
            ],
        }

    def iter_wallet_transactions(self, after_id: int, batch_size: int) -> Iterator[dict]:
        for batch in self.iter_pk_range("wallet_transactions", "id", after_id, batch_size):
            yield from batch

    def users_by_ids(self, user_ids: list[int]) -> dict[int, dict]:
        if not user_ids:
            return {}
        placeholders = ",".join(["%s"] * len(user_ids))
        rows = self.fetchall(f"SELECT * FROM users WHERE id IN ({placeholders})", tuple(user_ids))
        return {int(r["id"]): r for r in rows}
