"""Build and solve a year's roster from the command line.

    python3.13 -m roster_builder.build \
        --leave "Leave projection_master 2026_Pre.xlsx" \
        --targets StaffList_Targets.xlsx \
        --template "Consultant Roster Empty.xlsx" \
        --out "Consultant Roster 2026.xlsx"

Writes the roster to --out and the leave projection with duties entered to
--leave-out (default: the leave file's name ending in _Clean). With --blank,
only the empty template is written. --previous (last year's roster) fills in
the week carried forward into January.

The holiday list is printed for checking before anything is written; this is
the step the UI will turn into a confirmation table.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

if not __package__:  # run as a file (e.g. VS Code's Run button): make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "roster_builder"

from .carried import read_carried
from .holidays import fetch, from_fallback, span_extra
from .leave import read_leave, read_year
from .model import solve
from .staff import BANDS, read_staff, unlisted
from .template import build_layout, write_template
from .writers import fill_leave, fill_roster


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build and solve a consultant roster for a year.")
    ap.add_argument("--leave", required=True, type=Path, help="leave projection workbook")
    ap.add_argument("--targets", required=True, type=Path, help="StaffList_Targets.xlsx")
    ap.add_argument("--template", required=True, type=Path, help="Consultant Roster Empty.xlsx")
    ap.add_argument("--out", required=True, type=Path, help="roster workbook to write")
    ap.add_argument("--leave-out", type=Path, help="leave projection with duties entered")
    ap.add_argument("--previous", type=Path, help="last year's roster, for the carried-forward week")
    ap.add_argument("--year", type=int, help="override the year read from A1")
    ap.add_argument("--offline", action="store_true", help="skip the fetch, use the built-in list")
    ap.add_argument("--blank", action="store_true", help="only write the empty template")
    ap.add_argument("--seconds", type=float, default=180, help="solver time limit")
    ap.add_argument("--yes", action="store_true", help="do not pause to confirm")
    args = ap.parse_args(argv)
    leave_out = args.leave_out or args.leave.with_name(args.leave.stem.replace("_Pre", "") + "_Clean.xlsx")

    year = args.year or read_year(args.leave)
    print(f"Year: {year}" + ("" if args.year else f"  (from A1 of {args.leave.name})"))

    holidays = from_fallback(year) if args.offline else fetch(year)
    holidays = holidays + span_extra(year)
    print("\nPublic holidays - check the Monday in-lieu days:")
    for h in holidays:
        print(f"  {h.actual} {h.actual:%a}  {h.name:22} -> {h.observed or '-'}  {h.treatment}")

    staff = read_staff(args.targets)
    layout = build_layout(year, holidays)
    print(
        f"\nLayout: {layout.weeks} counted weeks + {layout.ph} PH rows"
        f" = {layout.slots} duties to fill ({len(layout.rows)} sheet rows)"
    )
    demand_min = sum(s.tmin for s in staff)
    demand_max = sum(s.tmax for s in staff)
    verdict = "feasible" if demand_min <= layout.slots <= demand_max else "NOT FEASIBLE"
    print(
        f"Staff: {len(staff)} rosterable; target capacity {demand_min}-{demand_max}"
        f"  vs {layout.slots} duties -> {verdict}"
    )

    leave = read_leave(args.leave, staff)
    print(f"\nLeave: covers {leave.first} to {leave.last}")
    if leave.unknown:
        print("  Unknown codes - treated as leave, please check:")
        for cell, name, code, _ in leave.unknown:
            print(f"    {cell:14} {name:30} {code!r}")
    new_names = unlisted(leave.others, args.targets)
    if new_names:
        print(f"\nIn the leave projection but not in the staff list (not rostered): {'; '.join(new_names)}")
        print("Add them to the Targets sheet with a short name and group if they should be.")
    if leave.missing:
        print(f"\nNot in the leave sheet, so their leave is unknown: {', '.join(leave.missing)}")
        print("Add their rows to every month sheet (or remove them from the targets) and re-run.")
        return 1

    carried = read_carried(args.previous, year, staff) if args.previous else None
    if carried:
        short = {s.full_name: s.short_name for s in staff}
        print(f"\nCarried forward from {args.previous.name}:")
        for day, team in [*carried.weeks.items(), *carried.ph.items()]:
            names = [short.get(n, n) if n else "-" for n in [team.ca, *team.rounds]]
            print(f"  {day:%a %d %b %Y}  CA {names[0]:14} rounds {', '.join(names[1:])}")
        for note in carried.notes:
            print(f"  {note}")
    else:
        print("\nNo --previous roster: the carried-forward week is left blank.")
    extra = [carried] if carried else []

    if not args.yes:
        question = "Write the template?" if args.blank else "Solve and write the roster?"
        if input(f"\n{question} [y/N] ").strip().lower() not in {"y", "yes"}:
            print("Aborted.")
            return 1

    if args.blank:
        write_template(year, holidays, staff, args.template, args.out)
        fill_roster(args.out, staff, *extra)
        print(f"Wrote {args.out}")
        return 0

    print(f"\nSolving (up to {args.seconds:.0f}s):")
    result = solve(year, holidays, staff, leave, carried.people if carried else set(), seconds=args.seconds)
    if result is None:
        print("No roster found - check the targets and leave above.")
        return 1
    report(result, staff)

    write_template(year, holidays, staff, args.template, args.out)
    fill_roster(args.out, staff, result, *extra)
    print(f"\nWrote {args.out}")
    clashes = fill_leave(args.leave, leave_out, result, *extra)
    print(f"Wrote {leave_out}")
    for clash in clashes:
        print(f"  NOT WRITTEN - cell already filled: {clash}")
    return 0


def report(result, staff) -> None:
    """One-screen summary of how well the roster meets the preferences."""
    tally = lambda counts: ", ".join(f"{n} x{k}" for k, n in sorted(Counter(counts).items()))
    pame = [s.full_name for s in staff if s.pame]
    print(f"\n{result.status}: smallest gap between duties {result.min_gap} weeks")
    print(f"  CA sets per PAME consultant: {tally(result.ca_count[p] for p in pame)}")
    print(f"  PH days per person:          {tally(result.ph_count.values())}")
    print(f"  PH substitutions:            {result.substitutions}")
    met, made = result.requests
    print(f"  'OK' requests met:           {met} of {made}")
    for group in sorted({s.group for s in staff}):
        low, high = BANDS[group]
        totals = (result.totals[s.full_name] for s in staff if s.group == group)
        print(f"  Group {group} ({low}-{high}) totals:   {tally(totals)}")


if __name__ == "__main__":
    sys.exit(main())
