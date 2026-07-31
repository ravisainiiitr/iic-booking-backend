"""Aggregate pytest JUnit XML + harness JSON into a dashboard HTML/JSON export."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from tests.analysis_platform.utils import HarnessReport, CheckResult, finish_report


def merge_junit_into_report(junit_path: Path, report: HarnessReport) -> HarnessReport:
    if not junit_path.exists():
        report.add(CheckResult(name="junit", status="skipped", detail=f"missing {junit_path}", category="report"))
        return report
    tree = ET.parse(junit_path)
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    for suite in suites:
        for case in suite.findall("testcase"):
            name = case.attrib.get("name", "unknown")
            classname = case.attrib.get("classname", "pytest")
            time_s = float(case.attrib.get("time") or 0) * 1000
            failure = case.find("failure")
            skipped = case.find("skipped")
            if failure is not None:
                status, detail = "failed", (failure.attrib.get("message") or failure.text or "")[:500]
            elif skipped is not None:
                status, detail = "skipped", (skipped.attrib.get("message") or "")[:500]
            else:
                status, detail = "passed", ""
            report.add(
                CheckResult(
                    name=name,
                    status=status,
                    duration_ms=time_s,
                    detail=detail,
                    category=classname.split(".")[-1][:80],
                )
            )
    return report


def write_dashboard(*, report_dir: Path, junit_path: Path | None = None, metrics: dict | None = None) -> dict:
    report_dir.mkdir(parents=True, exist_ok=True)
    report = HarnessReport(suite="analysis_platform", started_at=datetime.now(timezone.utc).isoformat())
    if junit_path:
        merge_junit_into_report(junit_path, report)
    if metrics:
        report.metrics.update(metrics)
    finish_report(report)
    html = report.write_html(report_dir / "dashboard.html")
    js = report.write_json(report_dir / "dashboard.json")
    junit_out = report.write_junit(report_dir / "dashboard-junit.xml")
    return {"html": str(html), "json": str(js), "junit": str(junit_out), "summary": report.to_dict()}
