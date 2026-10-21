"""Consultant roster app.

    cd "PAME Consultant Roster"
    python3.13 -m streamlit run roster_builder/app.py

Files -> Holidays -> Targets -> Solve. Each tab needs the one before it.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `roster_builder` imports

from roster_builder.carried import read_carried
from roster_builder.holidays import Holiday, download, fetch, span_extra
from roster_builder.leave import read_leave, read_year
from roster_builder.model import CA_MAX, CA_MIN, PH_MAX, R6_GROUPS, calendar, solve
from roster_builder.staff import BANDS, Staff, read_all, rosterable, unlisted, write_targets
from roster_builder.template import DAY, build_layout, write_template
from roster_builder.writers import fill_leave, fill_roster

EDITABLE = ["Short name", "Group", "CA calls", "Min", "Max"]
TEMPLATE_URL = "https://raw.githubusercontent.com/aiehtelac/consultantroster/main/Consultant%20Roster%20Empty.xlsx"

st.set_page_config("PAME Consultant Roster", layout="wide")
st.title("PAME Consultant Roster")
state = st.session_state
tmp = Path(state.setdefault("tmp", tempfile.mkdtemp()))


def saved(upload, name: str) -> Path:
    path = tmp / name
    path.write_bytes(upload.getvalue())
    return path


@st.cache_data(ttl=600)  # re-download at most every 10 minutes
def template_bytes() -> bytes:
    """The blank roster template, from GitHub."""
    return download(TEMPLATE_URL)


def staff_frame(staff: list[Staff]) -> pd.DataFrame:
    """Editable staff table; Min/Max stay blank when they match the group's range."""
    rows = []
    for s in staff:
        own = (s.tmin, s.tmax) != BANDS[s.group]
        rows.append({"Full name": s.full_name, "Short name": s.short_name, "Group": s.group,
                     "CA calls": s.pame, "Min": s.tmin if own else None, "Max": s.tmax if own else None})
    df = pd.DataFrame(rows).set_index("Full name")
    return df.astype({"Min": "Int64", "Max": "Int64"})


def frame_staff(df: pd.DataFrame) -> list[Staff]:
    out = []
    for name, r in df.iterrows():
        low, high = BANDS[r["Group"]]
        tmin = low if pd.isna(r["Min"]) else int(r["Min"])
        tmax = high if pd.isna(r["Max"]) else int(r["Max"])
        if tmin > tmax:
            raise ValueError(f"{name}: min {tmin} is above max {tmax}")
        short = "" if pd.isna(r["Short name"]) else str(r["Short name"]).strip()
        out.append(Staff(name, short, r["Group"], bool(r["CA calls"]), tmin, tmax))
    return out


files, hols_tab, targets_tab, solve_tab = st.tabs(["1 · Files", "2 · Holidays", "3 · Targets", "4 · Solve"])

# --- 1 · Files ----------------------------------------------------------------
with files:
    st.write("Upload staff leaves and targets here.")
    leave_up = st.file_uploader("Leave projection", type="xlsx")
    targets_up = st.file_uploader("Staff list and targets", type="xlsx")
    previous_up = st.file_uploader("Last year's Consultant Roster (optional: fills the week carried into January)", type="xlsx")
    if not (leave_up and targets_up):
        st.info("Upload the first two files to start.")
        st.stop()

    leave_path, targets_path = saved(leave_up, "leave.xlsx"), saved(targets_up, "targets.xlsx")
    template_path = tmp / "template.xlsx"
    try:
        template_path.write_bytes(template_bytes())
    except OSError as exc:
        st.error(f"Could not download the blank roster template from GitHub ({exc}). Check the internet connection.")
        st.stop()
    st.caption(f"Blank roster template: [Consultant Roster Empty.xlsx on GitHub]({TEMPLATE_URL})")
    try:
        if state.get("targets_id") != targets_up.file_id:  # new targets file: reload the table
            state.staff, state.targets_id = staff_frame(read_all(targets_path)), targets_up.file_id
        if state.get("leave_id") != (leave_up.file_id, targets_up.file_id):  # names come from the targets
            state.leave = read_leave(leave_path, read_all(targets_path))
            state.leave_year, state.leave_id = read_year(leave_path), (leave_up.file_id, targets_up.file_id)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()
    leave = state.leave

    year = int(st.number_input("Roster year (read from A1 of the leave projection - change if wrong)",
                               value=state.leave_year, step=1))
    st.write(f"Leave projection covers **{leave.first:%d %b %Y}** to **{leave.last:%d %b %Y}**.")
    new_names = unlisted(leave.others, targets_path)
    if new_names:
        st.warning("In the leave projection but not in the staff list, so they won't be rostered. "
                   "Add them to StaffList_Targets.xlsx (full name exactly as in the leave projection, with a "
                   "short name and group), then upload it again: " + "; ".join(new_names))
    rostered = set(state.staff.index[state.staff["Group"] != "E"])
    unknown = [u for u in leave.unknown if u[1] in rostered]
    freed = set()  # (name, date) of codes unticked as not leave
    if unknown:
        st.warning("Unknown leave codes - treated as leave. Untick any that are not leave (that day is then free):")
        codes = st.data_editor(
            pd.DataFrame([{"Valid leave": True, "Cell": c, "Name": n, "Code": code, "Date": f"{d:%a %d %b %Y}"}
                          for c, n, code, d in unknown]),
            hide_index=True, disabled=["Cell", "Name", "Code", "Date"], key="unknown_codes",
        )
        freed = {(n, d) for (_, n, _, d), valid in zip(unknown, codes["Valid leave"]) if not valid}
        if freed:  # a working copy, so re-ticking restores the original
            leave = replace(leave, blocked={n: {d for d in days if (n, d) not in freed}
                                            for n, days in leave.blocked.items()})

    carried = None
    if previous_up:
        try:
            carried = read_carried(saved(previous_up, "previous.xlsx"), year, frame_staff(state.staff))
        except ValueError as exc:
            st.error(str(exc))
        else:
            short = dict(zip(state.staff.index, state.staff["Short name"]))
            st.write("Carried forward from last year (shown on the roster, not counted):")
            st.dataframe(pd.DataFrame(
                [[f"{d:%a %d %b %Y}"] + [short.get(n, n) if n else "" for n in [t.ca, *t.rounds]]
                 for d, t in [*carried.weeks.items(), *carried.ph.items()]],
                columns=["Date", "CA", "R1", "R2", "R3", "R4", "R5", "R6"]), hide_index=True)
            for note in carried.notes:
                st.caption(note)

# --- 2 · Holidays ---------------------------------------------------------------
with hols_tab:
    st.write("Download MOM's list of public holidays and check they are accurate. "
             "Each PH is generally covered by that week's team.")
    if st.button("Import from MOM") or state.get("hol_year") != year:
        try:
            listed = fetch(year)
        except KeyError:
            listed = []
            st.warning(f"No list for {year} from MOM or built in - add the holidays below.")
        state.hol_df = pd.DataFrame({"Holiday": [h.name for h in listed], "Date": [h.actual for h in listed]})
        state.hol_year = year
        state.pop("hol_editor", None)

    st.write("Gazetted dates (add, edit or delete rows). Hari Raya dates may be provisional.")
    edited = st.data_editor(state.hol_df, num_rows="dynamic", key="hol_editor", hide_index=True,
                            column_config={"Date": st.column_config.DateColumn(format="ddd DD MMM YYYY")})
    holidays = [Holiday(str(r.Holiday).strip(), pd.Timestamp(r.Date).date())
                for r in edited.itertuples() if pd.notna(r.Date) and pd.notna(r.Holiday)]
    holidays += span_extra(year)

    st.write("How each one lands on the roster - check the Monday in-lieu days:")
    st.dataframe(pd.DataFrame([{"Date": f"{h.actual:%a %d %b %Y}", "Holiday": h.name,
                                "Rostered on": f"{h.observed:%a %d %b}" if h.observed else "-",
                                "Treatment": h.treatment} for h in sorted(holidays, key=lambda h: h.actual)]),
                 hide_index=True)

# --- 3 · Targets ------------------------------------------------------------------
with targets_tab:
    layout = build_layout(year, holidays)
    weeks, _ = calendar(year, holidays)

    st.markdown(
        f"""
You can edit staff's short names, groups, and target calls here.

| Group | CA calls (active PAME consultant) | Total calls per year |
|---|---|---|
| **A** | Yes - 1-3 weeks, target 2 | 6-7 |
| **B** | No | 6-7 |
| **C** | No | 4-5 |
| **D** | No | 1-3 |
| **E** | No | None |

1. You can edit a staff's short name, group and eligibility to do CA calls. To change the target number of calls to one different from the rest of the group, key in a new minimum/maximum total number of calls
2. Check the **Target* column or new *min/max* columns against *Free weekends* and *Free CA weeks*, which come from the leave projection.
3. Read the *Capacity* table below: every row needs ✅. Then press **Quick check**, which asks the
   solver whether a roster exists, and fix any red messages.
4. *Download updated targets* to keep your changes for next year. Personal ranges are saved in the
   *Target calls* column; anyone on their group's range is left blank.
"""
    )
    group = st.radio("Show group", ["All", *BANDS], horizontal=True)
    view = state.staff if group == "All" else state.staff[state.staff["Group"] == group]
    view = view.assign(
        Target=[f"{r.Min if pd.notna(r.Min) else BANDS[r.Group][0]}-{r.Max if pd.notna(r.Max) else BANDS[r.Group][1]}"
                for r in view.itertuples()],
        **{"Free weekends": [sum(leave.free(n, f + DAY, f + 2 * DAY) for f in weeks) if n in leave.blocked else None
                             for n in view.index],
           "Free CA weeks": [sum(leave.free(n, *(f + i * DAY for i in range(7))) for f in weeks)
                             if n in leave.blocked else None for n in view.index]},
    )
    key = f"staff_{group}"
    st.data_editor(view, key=key, width="stretch", disabled=["Target", "Free weekends", "Free CA weeks"],
                   column_order=["Short name", "Group", "CA calls", "Target", "Min", "Max",
                                 "Free weekends", "Free CA weeks"],
                   column_config={
                       "Group": st.column_config.SelectboxColumn(options=list(BANDS), required=True),
                       "Min": st.column_config.NumberColumn(min_value=0, step=1, help="Blank = group range"),
                       "Max": st.column_config.NumberColumn(min_value=0, step=1, help="Blank = group range"),
                   })
    changes = state[key]["edited_rows"]
    if changes:  # save into the full table, then redraw (a changed group can move a row out of view)
        for i, change in changes.items():
            for column, value in change.items():
                if column in EDITABLE:
                    state.staff.loc[view.index[int(i)], column] = value
        del state[key]
        st.rerun()

    try:
        everyone = frame_staff(state.staff)
        staff = rosterable(everyone)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

    st.subheader("Capacity")
    pame = [s for s in staff if s.pame]
    checks = [
        ("All duties (weekends + PHs)", layout.slots, sum(s.tmin for s in staff), sum(s.tmax for s in staff)),
        ("CA weeks", layout.weeks, len(pame) * CA_MIN, len(pame) * CA_MAX),
        ("R6 standby (groups A-C, max 1 each)", layout.weeks, 0, sum(s.group in R6_GROUPS for s in staff)),
        ("PH duties (max 2 each)", layout.ph * 7, 0, len(staff) * PH_MAX),
    ]
    st.dataframe(pd.DataFrame([{"Check": c, "Needed": n, "Targets allow": f"{lo}-{hi}",
                                "OK": "✅" if lo <= n <= hi else "❌"} for c, n, lo, hi in checks]),
                 hide_index=True)

    problems = [f"{s.full_name} is not in the leave projection" for s in staff if s.full_name in leave.missing]
    for s in staff:
        if s.full_name in leave.blocked:
            free = sum(leave.free(s.full_name, f + DAY, f + 2 * DAY) for f in weeks)
            if free < s.tmin:
                problems.append(f"{s.full_name}: min {s.tmin} but only {free} free weekends")
            if s.pame and not any(leave.free(s.full_name, *(f + i * DAY for i in range(7))) for f in weeks):
                problems.append(f"{s.full_name}: eligible for CA calls but no fully free Fri-Thurs week")
    for p in problems:
        st.error(p)
    st.caption("These checks are necessary but not sufficient - Quick check asks the solver itself.")

    col1, col2 = st.columns(2)
    if col1.button("Quick check (15 s)"):
        with st.spinner("Checking..."):
            quick = solve(year, holidays, staff, leave, carried.people if carried else set(), seconds=15, log=lambda _: None)
        if quick:
            col1.success("A roster exists with these targets.")
        else:
            col1.error("No roster found in 15 s - the targets or leave are too tight.")
    write_targets(targets_path, tmp / "targets_out.xlsx", everyone)
    col2.download_button("Download updated targets", (tmp / "targets_out.xlsx").read_bytes(),
                         file_name=targets_up.name)

# --- 4 · Solve ---------------------------------------------------------------------
with solve_tab:
    st.caption("This solver aims to fill calls around everyone's leave and within targets, while keeping PH teams together and spaces out duties.")
    if problems:
        st.error("Fix the problems on the Targets tab first.")
        st.stop()
    seconds = st.slider("Time limit (seconds)", 30, 300, 180, 30)
    if st.button("Solve", type="primary"):
        with st.spinner(f"Solving - up to {seconds} s..."):
            result = solve(year, holidays, staff, leave, carried.people if carried else set(),
                           seconds=seconds, log=lambda _: None)
        if result is None:
            st.error("No roster found - check the capacity panel on the Targets tab.")
            state.pop("result", None)
        else:
            extra = [carried] if carried else []
            roster_out, leave_out = tmp / "roster_out.xlsx", tmp / "leave_out.xlsx"
            write_template(year, holidays, staff, template_path, roster_out)
            fill_roster(roster_out, staff, result, *extra)
            clashes = fill_leave(leave_path, leave_out, result, *extra, not_leave=freed)
            state.result, state.outputs = result, (roster_out.read_bytes(), leave_out.read_bytes(), clashes)

    if "result" in state:
        result = state.result
        roster_bytes, leave_bytes, clashes = state.outputs
        st.success(f"{result.status} - {'best possible roster' if result.status == 'OPTIMAL' else 'best found in the time limit'}")
        met, made = result.requests
        c = st.columns(4)
        c[0].metric("Smallest gap between duties", f"{result.min_gap} weeks")
        c[1].metric("PH substitutions", result.substitutions)
        c[2].metric("'OK' requests met", f"{met} of {made}")
        c[3].metric("CA-eligible staff with 2 CA weeks", f"{sum(result.ca_count[s.full_name] == 2 for s in pame)} of {len(pame)}")
        st.dataframe(pd.DataFrame([{"Name": s.full_name, "Short": s.short_name, "Group": s.group, "Target": s.band,
                                    "Total": result.totals.get(s.full_name), "CA sets": result.ca_count.get(s.full_name),
                                    "PH days": result.ph_count.get(s.full_name)} for s in staff]),
                     hide_index=True, width="stretch")
        for clash in clashes:
            st.warning(f"Not written - cell already filled: {clash}")
        d1, d2 = st.columns(2)
        d1.download_button("Download Consultant Roster", roster_bytes, file_name=f"Consultant Roster {year}.xlsx")
        d2.download_button("Download leave projection (with duties)", leave_bytes,
                           file_name=Path(leave_up.name).stem.replace("_Pre", "") + "_Clean.xlsx")
