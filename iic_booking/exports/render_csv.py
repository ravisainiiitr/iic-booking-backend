"""CSV: UTF-8 with BOM (Excel opens ₹ and Hindi correctly), CRLF rows, formula-guarded text.

A single-table document is a plain CSV. Several tables are written one after another, each preceded by its
title and separated by a blank line.
"""

from __future__ import annotations

import csv
import io

from . import spec
from .values import display_text
from .values import guard_formula
from .values import raw_value
from .values import to_number


def _write_table(writer, table: spec.Table) -> None:
    writer.writerow([guard_formula(c.header) for c in table.columns])
    for row in table.rows:
        out = []
        for column in table.columns:
            text = display_text(raw_value(row, column), column.type, machine=True)
            numeric = column.type in spec.NUMERIC_TYPES and to_number(text.rstrip("%")) is not None
            out.append(text if numeric else guard_formula(text))
        writer.writerow(out)


def render_csv(document: spec.Document) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    tables = document.tables
    if len(tables) == 1:
        _write_table(writer, tables[0])
    else:
        for index, table in enumerate(tables):
            if index:
                writer.writerow([])
            writer.writerow([guard_formula(table.title)])
            _write_table(writer, table)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")
