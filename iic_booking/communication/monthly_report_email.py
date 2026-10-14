"""HTML / plain-text sections and the Excel attachment of the monthly usage report.

The sections are dropped into the ``monthly_usage_report_email`` template as ``{{ report_html }}`` /
``{{ report_text }}``. Every value is escaped here, and braces are neutralised because the template renderer strips
any leftover ``{{ }}`` / ``{% %}`` after substitution.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional, Sequence

from django.utils.html import escape

from .email_branding import (
    COLOR_ACCENT,
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_MUTED,
    COLOR_PRIMARY,
    COLOR_SOFT_PANEL_BG,
    COLOR_SOFT_PANEL_BORDER,
    COLOR_TEXT,
)
from .utils import get_frontend_absolute_url

FONT = "font-family:Arial,Helvetica,sans-serif;"
UP = "#15803d"
DOWN = "#b91c1c"


def _e(value: Any) -> str:
    return escape("" if value is None else str(value)).replace("{", "&#123;").replace("}", "&#125;")


def _t(value: Any) -> str:
    return ("" if value is None else str(value)).replace("{{", "{ {").replace("{%", "{ %")


def inr(value) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    return f"₹{amount:,.2f}"


def pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def num(value) -> str:
    if isinstance(value, float):
        return f"{value:,.1f}"
    return f"{int(value or 0):,}"


def delta(current, previous, *, kind: str = "num") -> tuple[str, str]:
    """('+12%' / '−3 pts' / 'new', colour) versus the month before; ('', '') when nothing to compare."""
    if current is None or previous is None:
        return "", COLOR_MUTED
    if kind == "pct":
        diff = (float(current) - float(previous)) * 100
        if abs(diff) < 0.05:
            return "no change", COLOR_MUTED
        return f"{'+' if diff > 0 else '−'}{abs(diff):.1f} pts", UP if diff > 0 else DOWN
    cur, prev = float(current), float(previous)
    if prev == 0:
        return ("new" if cur > 0 else "", UP if cur > 0 else COLOR_MUTED)
    change = (cur - prev) / prev * 100
    if abs(change) < 0.5:
        return "no change", COLOR_MUTED
    return f"{'+' if change > 0 else '−'}{abs(change):.0f}%", UP if change > 0 else DOWN


def link(path: str) -> str:
    return get_frontend_absolute_url(path) if path else ""


# --------------------------------------------------------------------------- HTML building blocks


def section_html(title: str, subtitle: str, inner: str) -> str:
    return f"""
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
       style="background:{COLOR_CARD};border:1px solid {COLOR_BORDER};border-radius:14px;margin:18px 0;">
  <tr>
    <td style="padding:16px 18px 4px 18px;{FONT}">
      <div style="font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:{COLOR_PRIMARY};">{_e(title)}</div>
      {f'<div style="font-size:12px;color:{COLOR_MUTED};margin-top:4px;">{_e(subtitle)}</div>' if subtitle else ''}
    </td>
  </tr>
  <tr><td style="padding:6px 18px 16px 18px;">{inner}</td></tr>
</table>"""


def kpis_html(items: Sequence[tuple[str, str, str, str]]) -> str:
    """Up to four key numbers per row: (label, value, change text, change colour)."""
    rows = []
    for i in range(0, len(items), 4):
        cells = []
        chunk = items[i : i + 4]
        for label, value, change, colour in chunk:
            change_html = (
                f'<div style="font-size:11px;font-weight:700;color:{colour};margin-top:2px;">{_e(change)} vs last month</div>'
                if change
                else '<div style="font-size:11px;margin-top:2px;">&nbsp;</div>'
            )
            cells.append(
                f'<td width="{100 // len(chunk)}%" style="padding:6px;vertical-align:top;">'
                f'<div style="background:#ffffff;border:1px solid {COLOR_BORDER};border-radius:10px;padding:10px 12px;{FONT}">'
                f'<div style="font-size:11px;color:{COLOR_MUTED};">{_e(label)}</div>'
                f'<div style="font-size:19px;font-weight:800;color:{COLOR_TEXT};margin-top:3px;">{_e(value)}</div>'
                f"{change_html}</div></td>"
            )
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin:0 -6px;">'
        f"{''.join(rows)}</table>"
    )


def table_html(heading: str, headers: Sequence[str], rows: Sequence[Sequence[Any]], *, numeric_from: int = 1,
               links: Optional[Sequence[str]] = None) -> str:
    if not rows:
        return ""
    th = "".join(
        f'<th align="{"left" if i < numeric_from else "right"}" style="padding:7px 6px;border-bottom:2px solid {COLOR_BORDER};'
        f'font-size:11px;font-weight:700;color:{COLOR_MUTED};text-transform:uppercase;letter-spacing:0.04em;">{_e(h)}</th>'
        for i, h in enumerate(headers)
    )
    body = []
    for r_index, row in enumerate(rows):
        tds = []
        for i, cell in enumerate(row):
            text = _e(cell)
            href = links[r_index] if (links and i == 0 and r_index < len(links)) else ""
            if href:
                text = f'<a href="{_e(href)}" style="color:{COLOR_ACCENT};text-decoration:none;font-weight:600;">{text}</a>'
            tds.append(
                f'<td align="{"left" if i < numeric_from else "right"}" style="padding:7px 6px;border-bottom:1px solid {COLOR_BORDER};'
                f'font-size:13px;color:{COLOR_TEXT};">{text}</td>'
            )
        body.append(f"<tr>{''.join(tds)}</tr>")
    title = (
        f'<div style="{FONT}font-size:12px;font-weight:700;color:{COLOR_TEXT};margin:14px 0 4px 0;">{_e(heading)}</div>'
        if heading
        else ""
    )
    return (
        f'{title}<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="{FONT}">'
        f"<tr>{th}</tr>{''.join(body)}</table>"
    )


def note_html(text: str) -> str:
    return (
        f'<div style="{FONT}margin:12px 0 0 0;padding:10px 12px;background:{COLOR_SOFT_PANEL_BG};'
        f'border:1px solid {COLOR_SOFT_PANEL_BORDER};border-radius:10px;font-size:12px;line-height:1.6;color:{COLOR_TEXT};">'
        f"{text}</div>"
    )


def links_html(items: Sequence[tuple[str, str]]) -> str:
    parts = [
        f'<a href="{_e(link(path))}" style="color:{COLOR_ACCENT};text-decoration:none;font-weight:700;">{_e(label)} &rarr;</a>'
        for label, path in items
        if link(path)
    ]
    if not parts:
        return ""
    return f'<div style="{FONT}font-size:13px;margin:14px 0 0 0;">{" &nbsp;&middot;&nbsp; ".join(parts)}</div>'


# --------------------------------------------------------------------------- sections


def faculty_html(s: dict[str, Any], month) -> str:
    cur, prev = s["current"], s["previous"]
    kpis = kpis_html(
        [
            ("Bookings", num(cur["bookings"]), *delta(cur["bookings"], prev["bookings"])),
            ("Hours booked", num(cur["hours"]), *delta(cur["hours"], prev["hours"])),
            ("Spend", inr(cur["spend"]), *delta(cur["spend"], prev["spend"])),
            ("Balance now", inr(s["balance_total"]), "", COLOR_MUTED),
        ]
    )
    parts = [kpis]
    if s["students_linked"]:
        parts.append(
            note_html(
                f"You: <strong>{_e(num(s['own']['bookings']))}</strong> bookings, {_e(inr(s['own']['spend']))}. "
                f"Your {_e(s['students_linked'])} linked student{'s' if s['students_linked'] != 1 else ''}: "
                f"<strong>{_e(num(cur['charged_bookings'] - s['own']['bookings']))}</strong> bookings, "
                f"{_e(inr(cur['spend'] - s['own']['spend']))}."
            )
        )
    parts.append(
        table_html(
            "By instrument",
            ["Instrument", "Bookings", "Hours", "Spend"],
            [[f"{r['name']} ({r['code']})" if r["code"] else r["name"], num(r["bookings"]), num(r["hours"]), inr(r["spend"])]
             for r in s["by_equipment"]],
        )
    )
    parts.append(
        table_html(
            "Wallet by sub-wallet",
            ["Sub-wallet", "Spent", "Refunded", "Recharged", "Balance"],
            [[r["name"], inr(r["debited"]), inr(r["refunded"]), inr(r["recharged"]), inr(r["balance"])]
             for r in s["sub_wallets"]],
        )
    )
    if s["top_students"]:
        parts.append(
            table_html(
                "Top users among your students",
                ["Student", "Bookings", "Hours", "Spend"],
                [[r["name"], num(r["bookings"]), num(r["hours"]), inr(r["spend"])] for r in s["top_students"]],
            )
        )
    if s["spending_limits"]:
        parts.append(
            table_html(
                f"Student spending limits ({month.label})",
                ["Student", "Spent", "Monthly limit", "Used"],
                [[r["name"], inr(r["spent"]), inr(r["limit"]), "—" if r["percent"] is None else f"{r['percent']}%"]
                 for r in s["spending_limits"]],
            )
        )
    extras = []
    if s["recharge_count"]:
        extras.append(
            f"{_e(s['recharge_count'])} recharge{'s' if s['recharge_count'] != 1 else ''} credited "
            f"({_e(inr(s['recharged_total']))})."
        )
    if s["results_awaiting"]:
        extras.append(
            f"<strong>{_e(s['results_awaiting'])}</strong> booking{'s have' if s['results_awaiting'] != 1 else ' has'} "
            "results waiting to be downloaded."
        )
    if extras:
        parts.append(note_html(" ".join(extras)))
    parts.append(
        links_html(
            [
                ("Reports & Statistics", s["links"]["reports"]),
                ("Wallet", s["links"]["wallet"]),
                ("My results", s["links"]["results"]),
            ]
        )
    )
    return section_html(
        "Your research group",
        "Your bookings and your linked students' bookings made in the month (charged bookings count towards spend).",
        "".join(parts),
    )


def _equipment_kpis(totals: dict[str, Any], previous: dict[str, Any], *, revenue: bool = True) -> str:
    items = [
        ("Bookings", num(totals["bookings"]), *delta(totals["bookings"], previous["bookings"])),
        ("Hours booked", num(totals["hours"]), *delta(totals["hours"], previous["hours"])),
        ("Utilisation", pct(totals["utilization"]), *delta(totals["utilization"], previous["utilization"], kind="pct")),
    ]
    if revenue:
        items.append(("Revenue", inr(totals["revenue"]), *delta(totals["revenue"], previous["revenue"])))
    return kpis_html(items)


def oic_html(s: dict[str, Any], month) -> str:
    t = s["totals"]
    parts = [_equipment_kpis(t, s["previous"])]
    parts.append(
        table_html(
            "Your equipment",
            ["Equipment", "Bookings", "Hours", "Utilisation", "Revenue", "Rating"],
            [
                [
                    f"{r['name']} ({r['code']})" if r["code"] else r["name"],
                    num(r["bookings"]),
                    num(r["hours"]),
                    pct(r["utilization"]),
                    inr(r["revenue"]),
                    "—" if r["avg_rating"] is None else f"{r['avg_rating']:.1f}/5",
                ]
                for r in s["table"]
            ],
            links=[link(r["link"]) for r in s["table"]],
        )
    )
    notes = []
    if t["cancellations"]:
        notes.append(
            f"{_e(t['cancellations'])} cancellation{'s' if t['cancellations'] != 1 else ''} "
            f"({_e(inr(t['refunded']))} refunded)."
        )
    if t["refunds"]:
        notes.append(f"{_e(t['refunds'])} booking{'s' if t['refunds'] != 1 else ''} fully refunded.")
    if t["disruptions"]:
        notes.append(f"{_e(t['disruptions'])} disruption{'s' if t['disruptions'] != 1 else ''} recorded.")
    if t["ratings"]:
        low = (
            f" <strong style=\"color:{DOWN};\">{_e(t['low_ratings'])} rated 2/5 or lower.</strong>" if t["low_ratings"] else ""
        )
        notes.append(f"{_e(t['ratings'])} rating{'s' if t['ratings'] != 1 else ''}, average {_e(t['avg_rating'])}/5.{low}")
    if t["pending"]:
        overdue = f", <strong style=\"color:{DOWN};\">{_e(t['overdue'])} overdue</strong>" if t["overdue"] else ""
        notes.append(f"Waiting on your lab now: <strong>{_e(t['pending'])}</strong> item{'s' if t['pending'] != 1 else ''}{overdue}.")
    if notes:
        parts.append(note_html("<br/>".join(notes)))
    if s.get("operators"):
        parts.append(
            table_html(
                "Lab Operator proficiency",
                ["Lab Operator", "Handled", "Pending", "Overdue", "Avg response", "Score"],
                [
                    [
                        r["name"],
                        num(r["handled"]),
                        num(r["pending"]),
                        num(r["overdue"]),
                        "—" if r["avg_response_hours"] is None else f"{r['avg_response_hours']:.1f} h",
                        "—" if r["score"] is None else f"{r['score']}",
                    ]
                    for r in s["operators"]
                ],
            )
        )
    parts.append(
        links_html(
            [
                ("Equipment Performance Report", s["links"]["report"]),
                ("Pending actions", s["links"]["pending"]),
                ("Disruption log", s["links"]["disruptions"]),
            ]
        )
    )
    return section_html(
        "Equipment you are Officer in Charge of",
        "Bookings with a slot in the month; revenue from completed bookings; utilisation counted from portal go-live "
        "(5 Oct 2026) within each equipment's weekly view window on working days.",
        "".join(parts),
    )


def hod_html(s: dict[str, Any], month) -> str:
    blocks = []
    for d in s["departments"]:
        if not d["has_activity"]:
            continue
        parts = []
        eq = d["equipment"]
        if eq and eq["totals"]["bookings"]:
            parts.append(_equipment_kpis(eq["totals"], eq["previous"]))
            parts.append(
                table_html(
                    "Top equipment",
                    ["Equipment", "Bookings", "Hours", "Utilisation", "Revenue"],
                    [
                        [f"{r['name']} ({r['code']})" if r["code"] else r["name"], num(r["bookings"]), num(r["hours"]),
                         pct(r["utilization"]), inr(r["revenue"])]
                        for r in d["top_equipment"]
                    ],
                )
            )
            parts.append(
                table_html(
                    "Who used the department's equipment",
                    ["User group", "Bookings", "Hours", "Revenue"],
                    [[r["name"], num(r["bookings"]), num(r["hours"]), inr(r["revenue"])] for r in d["user_groups"]],
                )
            )
        people = d["people"]
        if people["current"]["bookings"]:
            cur, prev = people["current"], people["previous"]
            change = delta(cur["bookings"], prev["bookings"])[0]
            parts.append(
                note_html(
                    f"People in the department made <strong>{_e(num(cur['bookings']))}</strong> bookings on any "
                    f"equipment ({_e(num(cur['users']))} users, {_e(num(cur['hours']))} h charged, "
                    f"{_e(inr(cur['spend']))}){f'; {_e(change)} vs last month' if change else ''}."
                )
            )
            parts.append(
                table_html(
                    "Equipment they used most",
                    ["Equipment", "Bookings", "Spend"],
                    [[f"{r['name']} ({r['code']})" if r["code"] else r["name"], num(r["bookings"]), inr(r["spend"])]
                     for r in people["top_equipment"]],
                )
            )
        blocks.append(section_html(f"Department: {d['name']}", "Head of Department summary", "".join(parts)))
    if blocks:
        blocks.append(links_html([("Open the portal", s["links"]["portal"])]))
    return "".join(blocks)


# --------------------------------------------------------------------------- plain text


def faculty_text(s: dict[str, Any], month) -> str:
    cur = s["current"]
    lines = [
        "YOUR RESEARCH GROUP",
        f"Bookings: {num(cur['bookings'])} | Hours: {num(cur['hours'])} | Spend: {inr(cur['spend'])} | "
        f"Balance now: {inr(s['balance_total'])}",
    ]
    for r in s["by_equipment"]:
        lines.append(f"- {_t(r['name'])}: {num(r['bookings'])} bookings, {num(r['hours'])} h, {inr(r['spend'])}")
    for r in s["sub_wallets"]:
        lines.append(f"Sub-wallet {_t(r['name'])}: spent {inr(r['debited'])}, recharged {inr(r['recharged'])}, "
                     f"balance {inr(r['balance'])}")
    if s["results_awaiting"]:
        lines.append(f"Results waiting to be downloaded: {s['results_awaiting']}")
    lines.append(f"Reports & Statistics: {link(s['links']['reports'])}")
    return "\n".join(lines)


def oic_text(s: dict[str, Any], month) -> str:
    t = s["totals"]
    lines = [
        "EQUIPMENT YOU ARE OFFICER IN CHARGE OF",
        f"Bookings: {num(t['bookings'])} | Hours: {num(t['hours'])} | Utilisation: {pct(t['utilization'])} | "
        f"Revenue: {inr(t['revenue'])}",
    ]
    for r in s["table"]:
        lines.append(f"- {_t(r['name'])}: {num(r['bookings'])} bookings, {pct(r['utilization'])} utilisation, "
                     f"{inr(r['revenue'])}")
    if t["pending"]:
        lines.append(f"Waiting on your lab now: {t['pending']} ({t['overdue']} overdue)")
    lines.append(f"Equipment Performance Report: {link(s['links']['report'])}")
    return "\n".join(lines)


def hod_text(s: dict[str, Any], month) -> str:
    lines = []
    for d in s["departments"]:
        if not d["has_activity"]:
            continue
        lines.append(f"DEPARTMENT: {_t(d['name'])}")
        if d["equipment"]:
            t = d["equipment"]["totals"]
            lines.append(f"Equipment bookings: {num(t['bookings'])} | Hours: {num(t['hours'])} | "
                         f"Utilisation: {pct(t['utilization'])} | Revenue: {inr(t['revenue'])}")
        cur = d["people"]["current"]
        if cur["bookings"]:
            lines.append(f"Bookings by the department's people: {num(cur['bookings'])} ({inr(cur['spend'])})")
    return "\n".join(lines)


# --------------------------------------------------------------------------- Excel


def build_excel(sections: dict[str, Any], month) -> Optional[bytes]:
    """Styled workbook (OIC equipment and / or HoD departments), or None when neither section has rows."""
    from io import BytesIO

    from openpyxl import Workbook

    from iic_booking.equipment.export_styles import SheetWriter, register_xlsx_styles, xlsx_text

    oic = sections.get("oic")
    hod = sections.get("hod")
    if not (oic and oic.get("has_activity")) and not (hod and hod.get("has_activity")):
        return None
    wb = Workbook(write_only=True)
    register_xlsx_styles(wb)
    headers = ["Equipment", "Code", "Bookings", "Hours", "Utilisation %", "Revenue", "Cancellations", "Refunded",
               "Disruptions", "Ratings", "Average rating", "Low ratings"]
    kinds = ["text", "text", "int", "text", "text", "money", "int", "money", "int", "int", "text", "int"]

    def equipment_sheet(title: str, table: list[dict[str, Any]]) -> None:
        sheet = SheetWriter(wb, title[:31])
        sheet.header(headers)
        for i, r in enumerate(table):
            sheet.row(
                [
                    xlsx_text(r["name"]),
                    xlsx_text(r["code"]),
                    r["bookings"],
                    r["hours"],
                    None if r["utilization"] is None else round(r["utilization"] * 100, 1),
                    float(r["revenue"]),
                    r["cancellations"],
                    float(r["refunded"]),
                    r["disruptions"],
                    r["ratings"],
                    r["avg_rating"],
                    r["low_ratings"],
                ],
                kinds,
                striped=bool(i % 2),
            )
        sheet.flush(autofilter_columns=len(headers))

    if oic and oic.get("has_activity"):
        equipment_sheet(f"OIC {month.key}", oic["table"])
    if hod and hod.get("has_activity"):
        for d in hod["departments"]:
            if d["equipment"] and d["equipment"]["table"]:
                equipment_sheet(f"{d['name'][:20]} {month.key}", d["equipment"]["table"])
            if d["user_groups"]:
                sheet = SheetWriter(wb, f"Groups {d['name'][:16]}"[:31])
                sheet.header(["User group", "Bookings", "Hours", "Revenue"])
                for i, r in enumerate(d["user_groups"]):
                    sheet.row([r["name"], r["bookings"], r["hours"], float(r["revenue"])],
                              ["text", "int", "text", "money"], striped=bool(i % 2))
                sheet.flush()
    if not wb.worksheets:
        return None
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
