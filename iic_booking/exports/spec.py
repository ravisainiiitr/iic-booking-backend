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
    """

    key: str
    header: str
    type: str = TEXT
    width: float = 1.0
    value: Callable[[dict], Any] | None = None
    visible: Callable[[Any], bool] | None = None
    total: bool = False

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
