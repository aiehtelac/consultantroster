"""Read the leave projection: who is unavailable on which dates.

Month sheets: A1 = first of the month, row 2 = day numbers from column B
(December runs on into the first days of January), names in column A from
row 4. A non-empty cell under a day means that person has something on.

Every code blocks a call. Known codes are taken silently; anything else still
blocks but is reported, so a typo can never put someone on call while away.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from openpyxl import load_workbook

from .staff import Staff, clean

# Leave codes that block calls (compared after normalising, see `normalise`)
BLOCKING = {
    "L", "C", "M", "PDL", "CL", "NS", "AM", "PM", "L PM", "T", "TL", "TC",
    "NR", "FC", "FCL", "CCL", "MC", "BL", "X", "OIL", "NO",
}
DUTIES = {"CA", "R1", "R2", "R3", "R4", "R5", "R6"}  # already-rostered calls, not leave
REQUEST = "OK"  # call request: available, not leave

FIRST_NAME_ROW = 4
END_MARKER = "TO DOUBLE CHECK"  # start of the formula block below the names


def normalise(code) -> str:
    """'l pm ' -> 'L PM', 'T/c' -> 'TC'."""
    return clean(code).upper().replace("/", "")


@dataclass
class Leave:
    blocked: dict[str, set[dt.date]] = field(default_factory=dict)  # full name -> dates
    requests: dict[str, set[dt.date]] = field(default_factory=dict)  # 'OK' dates
    unknown: list[tuple[str, str, str, dt.date]] = field(default_factory=list)  # (cell, name, code, date)
    missing: list[str] = field(default_factory=list)  # rosterable staff with no row
    others: list[str] = field(default_factory=list)  # names in the sheet but not in `staff` (incl. headings)
    first: dt.date | None = None  # dates the file covers
    last: dt.date | None = None

    def free(self, name: str, *days: dt.date) -> bool:
        """True if `name` has nothing on any of `days`."""
        return self.blocked[name].isdisjoint(days)


def month_sheets(wb):
    """Month sheets are the ones with a date in A1."""
    return [ws for ws in wb.worksheets if isinstance(ws["A1"].value, dt.datetime)]


def read_year(path) -> int:
    """Year from A1 of the first month sheet."""
    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        return month_sheets(wb)[0]["A1"].value.year
    finally:
        wb.close()


def column_dates(ws) -> dict[int, dt.date]:
    """Column number -> date, rolling into the next month when the day number drops."""
    month = ws["A1"].value.date().replace(day=1)
    out, prev = {}, 0
    for cell in ws[2][1:]:
        if not isinstance(cell.value, (int, float)):
            continue
        day = int(cell.value)
        if day < prev:  # e.g. Dec 31 -> Jan 1
            month = (month + dt.timedelta(days=32)).replace(day=1)
        out[cell.column] = month.replace(day=day)
        prev = day
    return out


def read_leave(path, staff: list[Staff]) -> Leave:
    """Blocked dates for every rosterable person, plus anything that needs checking."""
    wanted = {s.full_name for s in staff}
    leave = Leave(blocked={n: set() for n in wanted}, requests={n: set() for n in wanted})
    seen, covered, others = set(), set(), {}  # others: ordered, without repeats
    wb = load_workbook(path, data_only=True)

    for ws in month_sheets(wb):
        dates = column_dates(ws)
        covered.update(dates.values())
        for row in ws.iter_rows(min_row=FIRST_NAME_ROW):
            name = clean(row[0].value)
            if name.upper().startswith(END_MARKER):
                break
            if name not in wanted:  # service headings, Group E, or someone new
                if name:
                    others[name] = None
                continue
            seen.add(name)
            for cell in row[1:]:
                code = normalise(cell.value)
                if not code or cell.column not in dates or code in DUTIES:
                    continue
                if code == REQUEST:
                    leave.requests[name].add(dates[cell.column])
                    continue
                leave.blocked[name].add(dates[cell.column])
                if code not in BLOCKING:
                    cell_ref = f"{ws.title}!{cell.coordinate}"
                    leave.unknown.append((cell_ref, name, clean(cell.value), dates[cell.column]))

    leave.missing = sorted(wanted - seen)
    leave.others = list(others)
    leave.first, leave.last = min(covered), max(covered)
    return leave
