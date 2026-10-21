"""Build a blank consultant-roster workbook for a given year.

Layout, derived from the 2026 roster and reproduced exactly:

  rows 1-3    headers (kept from the template)
  row  4 ..   one row per week (Fri -> following Thu), each followed by the
              public holidays in it. A PH on the Friday itself goes above its
              week row.
  then        whatever the template has (blank rows, the "Col 1..10" labels),
              down to the "Calls Tally" header in column A. Below it: one row
              per rosterable consultant, a column-sum row, a control row and
              a deficit row.

The first week row is the week carried forward from the previous year. It and
any public holiday inside it are excluded from the tallies, so the COUNTIF
ranges start below them.
"""

from __future__ import annotations

import datetime as dt
from copy import copy
from dataclasses import dataclass

from openpyxl import load_workbook
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter

from .holidays import Holiday
from .staff import Staff, clean

A, B, D, E, L, M, P = 1, 2, 4, 5, 12, 13, 16
CA_COLS = (3, 6)  # C and F: the CA (weekday and weekend call)
ROUND_COLS = range(7, 13)  # G-L: Passive 1-6, L being the R6 standby
FIRST_ROW = 4  # first date row; the template's empty row 4 is the style donor
PASSIVE_SLOTS = 6  # R1..R6, all mandatory
DAY = dt.timedelta(days=1)

PH_FILL = PatternFill("solid", fgColor="BFBFBF")
PH_WEEK_FILL = PatternFill("solid", fgColor="C5D9F1")


@dataclass
class Row:
    """One date row: what goes in columns A, B, D and E, its fill and control number."""

    a: object
    b: object
    d: object
    e: object
    fill: PatternFill | None = None
    ctrl: int | None = None


@dataclass
class Layout:
    rows: list[Row]
    weeks: int  # counted weeks (excludes the carried-forward first week)
    ph: int  # counted PH rows
    carried: int  # rows belonging to the carried-forward week

    @property
    def first_counted_row(self) -> int:
        return FIRST_ROW + self.carried

    @property
    def last_row(self) -> int:
        return FIRST_ROW + len(self.rows) - 1

    @property
    def slots(self) -> int:
        """Total duties to fill: CA + R1..R6 on every counted row."""
        return (self.weeks + self.ph) * (1 + PASSIVE_SLOTS)


def as_date(value) -> dt.date | None:
    """A cell's date, or None if it doesn't hold one."""
    return value.date() if isinstance(value, dt.datetime) else None


def week_start(day: dt.date) -> dt.date:
    """The Friday that starts the roster week containing `day`."""
    return day - (day.weekday() - 4) % 7 * DAY


def fridays(year: int) -> list[dt.date]:
    """Every week start from the one containing 1 Jan to the one containing
    1 Jan of the following year.

    The first is the week carried forward from last year; the last is the week
    that next year's roster will carry forward, so consecutive years overlap by
    exactly one row and nothing is double-counted.
    """
    first, last = week_start(dt.date(year, 1, 1)), week_start(dt.date(year + 1, 1, 1))
    return [first + 7 * i * DAY for i in range((last - first).days // 7 + 1)]


def build_layout(year: int, holidays: list[Holiday]) -> Layout:
    """Each week row with its PH rows, plus the two control-number sequences."""
    weeks = fridays(year)
    by_week: dict[dt.date, list[Holiday]] = {fri: [] for fri in weeks}
    for h in sorted(holidays, key=lambda h: h.actual):
        fri = week_start(h.observed or h.actual)
        if fri in by_week:  # else outside the rostered span
            by_week[fri].append(h)

    rows: list[Row] = []
    week_n = ph_n = carried = 0
    for fri in weeks:
        hols = by_week[fri]
        sat = next((h.saturday_label() for h in hols if h.absorbed), fri + DAY)
        week = Row(fri, fri + 6 * DAY, sat, fri + 2 * DAY, PH_WEEK_FILL if hols else None)
        ph_rows = [
            Row(h.observed, h.label(), h.observed, h.label(), PH_FILL)
            for h in hols
            if not h.absorbed  # a Saturday PH only annotates the weekend
        ]
        block = [r for r in ph_rows if r.a == fri] + [week] + [r for r in ph_rows if r.a != fri]

        if fri == weeks[0]:  # carried forward: shown but not counted
            carried = len(block)
        else:
            week_n += 1
            week.ctrl = week_n
            for r in ph_rows:
                ph_n += 1
                r.ctrl = ph_n
        rows += block

    return Layout(rows, week_n, ph_n, carried)


def _grab_style(ws, row: int):
    return [copy(ws.cell(row, c)._style) for c in range(1, P + 2)], ws.row_dimensions[row].height


def _set_style(ws, row: int, style) -> None:
    cells, height = style
    for col, s in enumerate(cells, start=1):
        ws.cell(row, col)._style = copy(s)
    if height is not None:
        ws.row_dimensions[row].height = height


def write_template(year: int, holidays: list[Holiday], staff: list[Staff], src_path, dst_path) -> Layout:
    """Write the blank roster for `year` into a copy of the template."""
    layout = build_layout(year, holidays)
    wb = load_workbook(src_path)
    ws = wb.worksheets[0]

    # --- date block -------------------------------------------------------
    style = _grab_style(ws, FIRST_ROW)
    ws.insert_rows(FIRST_ROW + 1, len(layout.rows) - 1)
    for r, row in enumerate(layout.rows, start=FIRST_ROW):
        _set_style(ws, r, style)
        for col, value in ((A, row.a), (B, row.b), (D, row.d), (E, row.e), (M, row.ctrl)):
            ws.cell(r, col).value = value
        if row.fill:
            for col in range(A, L + 1):  # column M keeps its own yellow fill
                ws.cell(r, col).fill = row.fill

    # --- tally block ------------------------------------------------------
    header = next(
        (r for r in range(layout.last_row + 1, ws.max_row + 1) if clean(ws.cell(r, A).value).lower() == "calls tally"),
        None,
    )
    if header is None:
        raise ValueError("template has no 'Calls Tally' row in column A below the dates")
    first_person = header + 1

    style = _grab_style(ws, first_person)
    ws.insert_rows(first_person + 1, len(staff) - 1)
    lo, hi = layout.first_counted_row, layout.last_row
    for r, person in enumerate(staff, start=first_person):
        _set_style(ws, r, style)
        values = (
            [person.full_name]
            # B-H: CA and Passive 1-6 count grid columns F-L; the name is in column O
            + [f"=COUNTIF(${c}${lo}:${c}${hi},$O{r})" for c in "FGHIJKL"]
            + [
                f"=SUM(B{r}:H{r})",
                f'=COUNTIF($F${lo}:$F${hi},$O{r}&"-PH")',
                f'=COUNTIF($G${lo}:$L${hi},$O{r}&"-PH")',
                f"=SUM(J{r}:K{r})",
                f"=SUM(I{r}+L{r})",
                person.band,
                person.short_name,
            ]
        )
        for col, value in enumerate(values, start=A):
            ws.cell(r, col).value = value

    # --- sum, control and deficit rows (columns B-M) -----------------------
    last_person = first_person + len(staff) - 1
    sums, control, deficit = last_person + 1, last_person + 2, last_person + 4
    if not clean(ws.cell(control, A).value).lower().startswith("control number"):
        raise ValueError("template's tally should be: one name row, a sum row, then 'control number' in column A")
    # What a complete roster must contain, now that R6 is mandatory.
    control_values = [layout.weeks] * (1 + PASSIVE_SLOTS) + [
        f"=SUM(B{control}:H{control})",
        layout.ph,
        layout.ph * PASSIVE_SLOTS,
        f"=J{control}+K{control}",
        f"=I{control}+L{control}",
    ]
    for col, value in enumerate(control_values, start=B):
        x = get_column_letter(col)
        ws.cell(sums, col).value = f"=SUM({x}{first_person}:{x}{last_person})"
        ws.cell(control, col).value = value
        ws.cell(deficit, col).value = f"={x}{control}-{x}{sums}"

    wb.save(dst_path)
    return layout
