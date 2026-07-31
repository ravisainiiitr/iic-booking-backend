"""Harness utilities: assertions, cleanup, reporting."""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class CheckResult:
    name: str
    status: str  # passed | failed | skipped
    duration_ms: float = 0.0
    detail: str = ""
    category: str = "general"


@dataclass
class HarnessReport:
    suite: str = "analysis_platform"
    started_at: str = ""
    finished_at: str = ""
    checks: list[CheckResult] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def add(self, check: CheckResult) -> None:
        self.checks.append(check)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.status == "failed")

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.checks if c.status == "skipped")

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "total": len(self.checks),
            "metrics": self.metrics,
            "checks": [c.__dict__ for c in self.checks],
        }

    def write_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    def write_junit(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        suite = ET.Element(
            "testsuite",
            name=self.suite,
            tests=str(len(self.checks)),
            failures=str(self.failed),
            skipped=str(self.skipped),
        )
        for c in self.checks:
            case = ET.SubElement(
                suite,
                "testcase",
                classname=c.category,
                name=c.name,
                time=f"{c.duration_ms / 1000.0:.3f}",
            )
            if c.status == "failed":
                fail = ET.SubElement(case, "failure", message=c.detail or "failed")
                fail.text = c.detail
            elif c.status == "skipped":
                ET.SubElement(case, "skipped", message=c.detail or "skipped")
        ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)
        return path

    def write_html(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for c in self.checks:
            color = {"passed": "#0a7", "failed": "#c33", "skipped": "#888"}.get(c.status, "#333")
            rows.append(
                f"<tr><td>{c.category}</td><td>{c.name}</td>"
                f"<td style='color:{color}'>{c.status}</td>"
                f"<td>{c.duration_ms:.0f}ms</td><td>{_esc(c.detail)}</td></tr>"
            )
        metrics = "".join(f"<li><b>{_esc(k)}</b>: {_esc(str(v))}</li>" for k, v in self.metrics.items())
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>Analysis Platform Test Report</title>
<style>
body{{font-family:Segoe UI,system-ui,sans-serif;margin:24px;color:#222}}
table{{border-collapse:collapse;width:100%;margin-top:16px}}
th,td{{border:1px solid #ddd;padding:8px;text-align:left;font-size:14px}}
th{{background:#f5f5f5}}
.stat{{display:inline-block;margin-right:16px;padding:8px 12px;border:1px solid #ddd}}
</style></head><body>
<h1>Analysis Platform Test Harness</h1>
<p>Started: {self.started_at} · Finished: {self.finished_at}</p>
<div>
<span class="stat">Passed: <b>{self.passed}</b></span>
<span class="stat">Failed: <b>{self.failed}</b></span>
<span class="stat">Skipped: <b>{self.skipped}</b></span>
<span class="stat">Total: <b>{len(self.checks)}</b></span>
</div>
<h2>Metrics</h2><ul>{metrics or "<li>None</li>"}</ul>
<h2>Results</h2>
<table><thead><tr><th>Category</th><th>Check</th><th>Status</th><th>Duration</th><th>Detail</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
</body></html>"""
        path.write_text(html, encoding="utf-8")
        return path


def _esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def timed_check(report: HarnessReport, name: str, category: str, fn) -> CheckResult:
    started = time.perf_counter()
    try:
        detail = fn() or ""
        status = "passed"
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        status = "failed"
    ms = (time.perf_counter() - started) * 1000
    check = CheckResult(name=name, status=status, duration_ms=ms, detail=str(detail), category=category)
    report.add(check)
    return check


def new_report(suite: str = "analysis_platform") -> HarnessReport:
    return HarnessReport(suite=suite, started_at=datetime.now(timezone.utc).isoformat())


def finish_report(report: HarnessReport) -> HarnessReport:
    report.finished_at = datetime.now(timezone.utc).isoformat()
    return report


def assert_status(response, expected: int | set[int], *, context: str = "") -> None:
    allowed = {expected} if isinstance(expected, int) else set(expected)
    if response.status_code not in allowed:
        body = getattr(response, "data", None) or getattr(response, "content", b"")
        raise AssertionError(f"{context} expected {allowed}, got {response.status_code}: {body}")


def assert_no_hostname(payload: Any) -> None:
    """Researcher-facing payloads must not expose workstation identity (S1)."""
    if not isinstance(payload, dict):
        return
    reservation = payload.get("reservation") or {}
    if isinstance(reservation, dict):
        if reservation.get("workstation"):
            raise AssertionError("Researcher payload leaked reservation.workstation hostname")
        if reservation.get("workstation_id"):
            raise AssertionError("Researcher payload leaked workstation_id")
    analyze = payload.get("analyze") or {}
    if isinstance(analyze, dict):
        nested = analyze.get("reservation") or {}
        if isinstance(nested, dict) and (nested.get("workstation") or nested.get("workstation_id")):
            raise AssertionError("Analyze payload leaked workstation identity")
