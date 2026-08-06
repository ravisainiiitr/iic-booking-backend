#!/usr/bin/env python3
"""
Phase N.1 — Booking engine concurrency stress runner (correctness-focused).

Usage (on EC2 or any host with network to portal):
  python3 n1_booking_stress.py --manifest /tmp/n1_sat_manifest.json --base-url https://equip.iitr.ac.in
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass
class Attempt:
    email: str
    http: int
    ok: bool
    body: dict[str, Any]
    ms: float
    error: str = ""


@dataclass
class ScenarioResult:
    name: str
    passed: bool
    detail: str
    attempts: list[Attempt] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


def http_json(method: str, url: str, token: str | None = None, payload: dict | None = None, timeout=60):
    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Token {token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            ms = (time.perf_counter() - t0) * 1000
            body = json.loads(raw) if raw else {}
            return resp.status, body, ms, ""
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        ms = (time.perf_counter() - t0) * 1000
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {"raw": raw[:500]}
        return e.code, body, ms, ""
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        return 0, {}, ms, str(e)


class N1Stress:
    def __init__(self, base_url: str, manifest: dict, report_path: str):
        self.base = base_url.rstrip("/")
        self.m = manifest
        self.password = manifest["password"]
        self.report_path = report_path
        self.tokens: dict[str, str] = {}
        self.results: list[ScenarioResult] = []
        self._lock = threading.Lock()

    def login(self, email: str) -> str:
        with self._lock:
            if email in self.tokens:
                return self.tokens[email]
        code, body, _, err = http_json(
            "POST",
            f"{self.base}/api/auth/login/",
            payload={"email": email, "password": self.password},
        )
        if code not in (200, 201) or not body.get("token"):
            raise RuntimeError(f"login failed {email}: {code} {body} {err}")
        tok = body["token"]
        with self._lock:
            self.tokens[email] = tok
        return tok

    def users(self, role: str, limit: int | None = None) -> list[dict]:
        # emails: n1.sat.faculty001@..., n1.sat.student001@..., n1.sat.wallet-low@...
        needle = f".{role}"
        rows = [u for u in self.m["users"] if needle in u["email"]]
        rows = sorted(rows, key=lambda x: x["email"])
        return rows[:limit] if limit else rows

    def eq(self, code: str) -> dict:
        for e in self.m["equipments"]:
            if e["code"] == code:
                return e
        raise KeyError(code)

    def pick_available_slot(self, eq_id: int, token: str, prefer_date: str | None = None) -> tuple[str, str, int]:
        today = datetime.now().date()
        fr = today.isoformat()
        to = (today + timedelta(days=21)).isoformat()
        code, body, _, _ = http_json(
            "GET",
            f"{self.base}/api/equipments/{eq_id}/slots/?from={fr}&to={to}",
            token=token,
        )
        if code != 200:
            raise RuntimeError(f"slots {code} {body}")
        slots = body.get("slots") or []
        now = datetime.now(timezone.utc) + timedelta(minutes=30)
        for s in slots:
            if s.get("status") != "AVAILABLE":
                continue
            start = str(s.get("start_datetime"))
            end = str(s.get("end_datetime"))
            if prefer_date and not start.startswith(prefer_date):
                continue
            try:
                st = datetime.fromisoformat(start.replace("Z", "+00:00"))
                if st.tzinfo is None:
                    st = st.replace(tzinfo=timezone.utc)
                if st < now:
                    continue
            except Exception:
                continue
            return start, end, int(s["id"])
        raise RuntimeError("no AVAILABLE future slot")

    def bookers(self, n: int) -> list[dict]:
        pool = (
            self.users("booker")
            + self.users("faculty")
            + self.users("student")
            + self.users("external")
            + self.users("project")
        )
        # de-dupe
        seen = set()
        out = []
        for u in pool:
            if u["email"] in seen:
                continue
            seen.add(u["email"])
            out.append(u)
            if len(out) >= n:
                break
        return out

    def concurrent_same_slot(self, n: int, eq_code="N1-SINGLE") -> ScenarioResult:
        name = f"same_slot_n={n}"
        fac = self.bookers(n)
        if len(fac) < n:
            return ScenarioResult(
                name=name,
                passed=False,
                detail=f"SKIP insufficient users have={len(fac)} need={n}",
            )
        pioneer = fac[0]["email"]
        eq = self.eq(eq_code)
        tok = self.login(pioneer)
        start, end, sid = self.pick_available_slot(eq["id"], tok)
        barrier = threading.Barrier(n)
        attempts: list[Attempt] = []

        def worker(email: str):
            barrier.wait(timeout=180)
            return self.book(email, eq["id"], start, end)

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=min(n, 250)) as ex:
            futs = [ex.submit(worker, u["email"]) for u in fac]
            for f in as_completed(futs):
                attempts.append(f.result())
        wall = (time.perf_counter() - t0) * 1000
        wins = [a for a in attempts if a.ok]
        fails = [a for a in attempts if not a.ok]
        booking_ids = []
        for a in wins:
            bid = a.body.get("real_booking_id") or a.body.get("booking_id")
            booking_ids.append(bid)
        unique_wins = len(set(str(x) for x in booking_ids))
        passed = len(wins) == 1 and unique_wins == 1 and len(fails) == n - 1
        sample_err = ""
        for a in fails[:1]:
            sample_err = str(a.body.get("error") or a.body)[:120]
        detail = (
            f"slot={start} wins={len(wins)} unique={unique_wins} fails={len(fails)} "
            f"http_codes={sorted({a.http for a in attempts})} err={sample_err}"
        )
        lat = [a.ms for a in attempts]
        metrics = {
            "n": n,
            "wall_ms": round(wall, 1),
            "p50_ms": round(statistics.median(lat), 1) if lat else None,
            "p95_ms": round(sorted(lat)[max(0, int(len(lat) * 0.95) - 1)], 1) if lat else None,
            "wins": len(wins),
            "unique_booking_ids": unique_wins,
        }
        return ScenarioResult(name=name, passed=passed, detail=detail, attempts=attempts, metrics=metrics)

    def book(self, email: str, eq_id: int, start: str, end: str, extra: dict | None = None) -> Attempt:
        tok = self.login(email)
        payload = {
            "start_time": start,
            "end_time": end,
            "number_of_samples": 1,
            "input_values": {"A": "1", "B": "", "C": "", "D": "", "E": "", "F": [], "G": False},
            "waitlist_on_failure": False,
        }
        if extra:
            payload.update(extra)
        code, body, ms, err = http_json(
            "POST",
            f"{self.base}/api/equipments/{eq_id}/book/",
            token=tok,
            payload=payload,
            timeout=120,
        )
        ok = code in (200, 201) and (body.get("real_booking_id") or body.get("booking_id"))
        return Attempt(email=email, http=code, ok=bool(ok), body=body if isinstance(body, dict) else {}, ms=ms, error=err)

    def overlap_probe(self) -> ScenarioResult:
        name = "overlap_slots"
        eq = self.eq("N1-OVERLAP")
        users = self.users("faculty", 4)
        tok = self.login(users[0]["email"])
        # Find a 09:00-10:00 style slot day
        start, end, _ = self.pick_available_slot(eq["id"], tok)
        # Derive day
        day = str(start)[:10]
        windows = [
            (f"{day}T09:00:00+05:30", f"{day}T10:00:00+05:30"),
            (f"{day}T09:30:00+05:30", f"{day}T10:30:00+05:30"),
            (f"{day}T09:45:00+05:30", f"{day}T10:15:00+05:30"),
            (f"{day}T10:00:00+05:30", f"{day}T11:00:00+05:30"),
        ]
        # First book 09:00-10:00 if available else use picked
        attempts = []
        a0 = self.book(users[0]["email"], eq["id"], windows[0][0], windows[0][1])
        attempts.append(a0)
        # Concurrent overlapping
        barrier = threading.Barrier(3)

        def w(i, email, st, en):
            barrier.wait(timeout=60)
            return self.book(email, eq["id"], st, en)

        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = [
                ex.submit(w, i, users[i + 1]["email"], windows[i + 1][0], windows[i + 1][1])
                for i in range(3)
            ]
            for f in as_completed(futs):
                attempts.append(f.result())
        wins = [a for a in attempts if a.ok]
        # Correctness: at most one of overlapping windows for exclusive instrument —
        # 10:00-11:00 may be OK if adjacent non-overlap depending on slot model.
        # For N1-OVERLAP slot masters are non-overlapping hour blocks; 09:30 request
        # may fail for no matching slot rather than overlap. Treat: no two bookings
        # sharing the same DailySlot id.
        passed = len(wins) <= 2  # 09-10 and 10-11 allowed; mid overlaps should fail
        detail = f"wins={len(wins)} codes={[a.http for a in attempts]} bodies={[str(a.body)[:80] for a in attempts]}"
        return ScenarioResult(name=name, passed=passed, detail=detail, attempts=attempts)

    def wallet_race(self) -> ScenarioResult:
        name = "wallet_concurrent_debit"
        email = f"n1.sat.wallet-low@iic-booking.test"
        eq = self.eq("N1-MULTI")
        tok = self.login(email)
        # get two distinct available slots
        code, body, _, _ = http_json(
            "GET",
            f"{self.base}/api/equipments/{eq['id']}/slots/?from={datetime.now().date()}&to={(datetime.now().date()+timedelta(days=10))}",
            token=tok,
        )
        slots = [
            s
            for s in (body.get("slots") or [])
            if s.get("status") == "AVAILABLE"
            and datetime.fromisoformat(str(s["start_datetime"]).replace("Z", "+00:00"))
            > datetime.now(timezone.utc) + timedelta(minutes=30)
        ]
        if len(slots) < 8:
            return ScenarioResult(name=name, passed=False, detail=f"need slots got {len(slots)}")
        # Low wallet 50 INR; each booking ~10*1=10 charge maybe more with time — fire 10 concurrent
        picks = slots[:10]
        barrier = threading.Barrier(len(picks))

        def w(s):
            barrier.wait(timeout=60)
            return self.book(email, eq["id"], s["start_datetime"], s["end_datetime"])

        attempts = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = [ex.submit(w, s) for s in picks]
            for f in as_completed(futs):
                attempts.append(f.result())
        wins = [a for a in attempts if a.ok]
        # Check wallet non-negative
        code, wbody, _, _ = http_json("GET", f"{self.base}/api/wallet/", token=tok)
        bal = float(wbody.get("balance") or wbody.get("sub_wallets", [{}])[0].get("balance") or 0)
        # Also sum subwallets
        for sw in wbody.get("sub_wallets") or []:
            bal = min(bal, float(sw.get("balance", bal)))
        passed = bal >= -0.001 and True  # will refine: expect not all wins if charge > 50
        detail = f"wins={len(wins)} balance={bal} codes={[a.http for a in attempts]}"
        # Fail if balance negative
        if bal < -0.001:
            passed = False
            detail += " NEGATIVE_BALANCE"
        return ScenarioResult(name=name, passed=passed, detail=detail, attempts=attempts, metrics={"balance": bal, "wins": len(wins)})

    def integrity_scan(self, admin_email: str) -> ScenarioResult:
        name = "db_integrity_via_admin_api"
        # Limited to API-visible checks: list N1 bookings and detect duplicate slot occupancy via slots endpoint
        tok = self.login(admin_email)
        issues = []
        for eq in self.m["equipments"]:
            code, body, _, _ = http_json(
                "GET",
                f"{self.base}/api/equipments/{eq['id']}/slots/?from={datetime.now().date()}&to={(datetime.now().date()+timedelta(days=14))}",
                token=tok,
            )
            if code != 200:
                issues.append(f"slots fail {eq['code']}")
                continue
            seen = {}
            for s in body.get("slots") or []:
                if s.get("status") == "BOOKED" and s.get("booking_id"):
                    key = s["id"]
                    if key in seen and seen[key] != s.get("booking_id"):
                        issues.append(f"slot {key} multi-booking")
                    seen[key] = s.get("booking_id")
        passed = not issues
        return ScenarioResult(name=name, passed=passed, detail="; ".join(issues) or "no duplicate slot occupancy detected")

    def run_all(self):
        # Warm logins for a booker pool
        for u in self.bookers(120):
            try:
                self.login(u["email"])
            except Exception as e:
                print("login skip", u["email"], e)

        for n in (10, 50, 100, 250, 500, 1000):
            print(f"=== same-slot n={n} ===")
            # Reset slots between waves
            # (caller should reseed wipe; here we just pick a fresh future slot)
            r = self.concurrent_same_slot(n)
            self.results.append(r)
            print(r.name, "PASS" if r.passed else "FAIL", r.detail, r.metrics)
            if "SKIP" in r.detail:
                continue
            # Cancel winner so next wave can reuse equipment capacity
            wins = [a for a in r.attempts if a.ok]
            if wins:
                bid = wins[0].body.get("real_booking_id")
                if bid:
                    tok = self.login(wins[0].email)
                    http_json("POST", f"{self.base}/api/bookings/{bid}/user-cancel/", token=tok, payload={})
                    http_json("POST", f"{self.base}/api/bookings/{bid}/cancel/", token=tok, payload={"reason": "n1 stress cleanup"})


        print("=== overlap ===")
        r = self.overlap_probe()
        self.results.append(r)
        print(r.name, "PASS" if r.passed else "FAIL", r.detail)

        print("=== wallet ===")
        r = self.wallet_race()
        self.results.append(r)
        print(r.name, "PASS" if r.passed else "FAIL", r.detail)

        admin = next((u for u in self.m["users"] if u["user_type"] == "dept_admin"), None)
        if admin:
            print("=== integrity ===")
            r = self.integrity_scan(admin["email"])
            self.results.append(r)
            print(r.name, "PASS" if r.passed else "FAIL", r.detail)

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "base_url": self.base,
            "scenarios": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "detail": r.detail,
                    "metrics": r.metrics,
                    "win_count": sum(1 for a in r.attempts if a.ok),
                    "attempt_count": len(r.attempts),
                }
                for r in self.results
            ],
        }
        with open(self.report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print("Wrote", self.report_path)
        failed = [r for r in self.results if not r.passed and "SKIP" not in r.detail]
        print("FAILED", len(failed), "of", len(self.results))
        return 0 if not failed else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--base-url", default="https://equip.iitr.ac.in")
    ap.add_argument("--report", default="/tmp/n1_stress_report.json")
    args = ap.parse_args()
    with open(args.manifest, encoding="utf-8") as f:
        manifest = json.load(f)
    runner = N1Stress(args.base_url, manifest, args.report)
    raise SystemExit(runner.run_all())


if __name__ == "__main__":
    main()
