"""In-process old-MySQL stand-in for isolated apply / idempotency tests. Never talks to AWS."""

from __future__ import annotations

from decimal import Decimal
from typing import Iterator


class FakeOldMySQLReader:
    def __init__(self, users: list[dict], wallets: dict[int, dict], transactions: list[dict], fail_after_id: int | None = None):
        self.users = users
        self.wallets = wallets
        self.transactions = sorted(transactions, key=lambda t: int(t["id"]))
        self.fail_after_id = fail_after_id
        self.probe = {
            "ok": True,
            "database": "admin",
            "configured_host": "fake",
            "configured_port": 0,
            "configured_database": "admin",
            "row_counts": {
                "users": len(users),
                "user_wallet": len(wallets),
                "wallet_transactions": len(transactions),
            },
        }

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def connection_probe(self) -> dict:
        return self.probe

    def duplicate_employee_ids(self) -> set[str]:
        seen: dict[str, int] = {}
        for u in self.users:
            emp = str(u.get("emp_id") or "").strip()
            if emp:
                seen[emp] = seen.get(emp, 0) + 1
        return {k for k, v in seen.items() if v > 1}

    def iter_users(self, batch_size: int = 500):
        yield from self.users

    def wallet_for_user(self, user_id: int) -> dict | None:
        return self.wallets.get(int(user_id))

    def user_ledger_totals(self, user_id: int) -> tuple[Decimal, Decimal]:
        credits = Decimal("0")
        debits = Decimal("0")
        for t in self.transactions:
            if int(t["user_id"]) != int(user_id):
                continue
            amt = Decimal(str(t["amount"]))
            if int(t["transaction_type"]) == 1:
                credits += amt
            elif int(t["transaction_type"]) == 2:
                debits += amt
        return credits, debits

    def wallets_by_user_id(self) -> dict[int, dict]:
        return {int(k): v for k, v in self.wallets.items()}

    def ledger_totals_by_user_id(self) -> dict[int, tuple[Decimal, Decimal]]:
        out: dict[int, tuple[Decimal, Decimal]] = {}
        for t in self.transactions:
            uid = int(t["user_id"])
            c, d = out.get(uid, (Decimal("0"), Decimal("0")))
            amt = Decimal(str(t["amount"]))
            if int(t["transaction_type"]) == 1:
                c += amt
            elif int(t["transaction_type"]) == 2:
                d += amt
            out[uid] = (c, d)
        return out

    def transaction_stream_stats(self, after_id: int) -> dict:
        ids = [int(t["id"]) for t in self.transactions if int(t["id"]) > after_id]
        if self.fail_after_id is not None:
            ids = [i for i in ids if i <= self.fail_after_id]
        return {
            "n": len(ids),
            "min_id": min(ids) if ids else None,
            "max_id": max(ids) if ids else None,
        }

    def iter_wallet_transactions(self, after_id: int, batch_size: int) -> Iterator[dict]:
        n = 0
        for t in self.transactions:
            tid = int(t["id"])
            if tid <= after_id:
                continue
            if self.fail_after_id is not None and tid > self.fail_after_id:
                raise ConnectionError("simulated interruption")
            yield t
            n += 1
            if n >= batch_size:
                n = 0

    def users_by_ids(self, user_ids: list[int]) -> dict[int, dict]:
        wanted = set(user_ids)
        return {int(u["id"]): u for u in self.users if int(u["id"]) in wanted}

    def discover_schema(self) -> dict:
        return {
            "tables": ["users", "user_wallet", "wallet_transactions", "booking"],
            "columns": {
                "users": ["id", "emp_id", "email", "name"],
                "user_wallet": ["id", "user_id", "balance"],
                "wallet_transactions": [
                    "id", "user_id", "amount", "balance", "transaction_type", "create_date", "description", "created_by"
                ],
            },
        }
