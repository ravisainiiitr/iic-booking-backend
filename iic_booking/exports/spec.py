"""Report export model: a document is a title, filters, optional KPI cards and one or more tables."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Callable

TEXT = "text"
INTEGER = "integer"
NUMBER = "number"
CURRENCY = "currency"
PERCENT = "percent"
DATE = "date"
DATETIME = "datetime"
BOOL = "bool"

COLUMN_TYPES = frozenset({TEXT, INTEGER, NUMBER, CURRENCY, PERCENT, DATE, DATETIME, BOOL})
NUMERIC_TYPES = frozenset({INTEGER, NUMBER, CURRENCY, PERCENT})


@dataclass
class Column:
    """One column. ``key`` is a dotted path into the row dict unless ``value`` computes the cell.

    ``percent`` values are fractions (0.25 is 25 %). ``width`` is a relative width hint used by the PDF
    (wide text columns get 2-3, short codes 0.6-0.8). ``visible`` hides the column for some viewers.
    ``link`` gives the portal path (or absolute URL) of the row's detail page; PDF and Excel cells link to it.
    Cells are centred; ``align="left"`` is for long free text such as comments.
    """

    key: str
    header: str
    type: str = TEXT
    width: float = 1.0
    value: Callable[[dict], Any] | None = None
    visible: Callable[[Any], bool] | None = None
    total: bool = False
    link: Callable[[dict], str | None] | None = None
    align: str = "center"

    def __post_init__(self):
        if self.type not in COLUMN_TYPES:
            raise ValueError(f"Unknown column type {self.type!r} for {self.key!r}")


@dataclass
class Table:
    key: str
    title: str
    columns: list[Column]
    rows: list[dict] = field(default_factory=list)
    note: str = ""
    sheet_name: str = ""
    empty_message: str = "No records match these filters."
    # Renderers add a leading S.No. column (as on the portal's tables) unless the table already has one.
    # Off for fixed breakdowns such as counts by status.
    serial: bool = True


SERIAL_KEY = "_sno"


def _adds_serial(table: Table) -> bool:
    return table.serial and bool(table.columns) and not any(c.key == SERIAL_KEY for c in table.columns)


def rendered_columns(table: Table) -> list[Column]:
    columns = list(table.columns)
    return [Column(SERIAL_KEY, "S.No.", INTEGER, 0.45), *columns] if _adds_serial(table) else columns


def serial_view(table: Table) -> tuple[list[Column], list[dict]]:
    """Columns and rows as rendered: with the S.No. column when the table wants one."""
    if not _adds_serial(table):
        return list(table.columns), table.rows
    rows = [{**row, SERIAL_KEY: index} for index, row in enumerate(table.rows, start=1)]
    return rendered_columns(table), rows


@dataclass
class Kpi:
    label: str
    value: Any
    type: str = TEXT
    hint: str = ""


@dataclass
class Document:
    title: str
    slug: str
    tables: list[Table]
    subtitle: str = ""
    filters: list[tuple[str, str]] = field(default_factory=list)
    kpis: list[Kpi] = field(default_factory=list)
    generated_by: str = ""
    department: str = ""
    landscape: bool | None = None

    @property
    def row_count(self) -> int:
        return sum(len(t.rows) for t in self.tables)
