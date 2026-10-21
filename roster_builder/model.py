"""Roster the year with OR-Tools CP-SAT.

Each counted week (Fri -> Thu) needs:
  CA      1 PAME-capable consultant, free all 7 days
  R1-R5   5 rounders, free Sat and Sun
  R6      1 standby from groups A-C, free Sat and Sun

Each public-holiday row is covered by its week's team. The CA is free all week
so always covers it; a rounder on leave that day is replaced by someone free.

Hard rules:
  every slot filled; one duty per person per week
  each person's total (weekends + PHs) inside their group band
  PAME staff do 1-3 CA weeks; weekend R6 at most once; at most 2 PHs
Preferences (one weighted objective, solved once within a time limit):
  CA twice each; PHs in one week only (CNY's two days count once);
  the week's team kept on its PHs; 'OK' call requests met; duties spread out
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ortools.sat.python import cp_model

from .holidays import Holiday
from .leave import Leave
from .staff import Staff
from .template import DAY, fridays, week_start

ROUNDERS = 5  # R1-R5
R6_GROUPS = {"A", "B", "C"}
CA_MIN, CA_AIM, CA_MAX = 1, 2, 3
PH_AIM, PH_MAX = 1, 2
SPACING_WEEKS = 4  # two duties inside any 4-week window are penalised

# Objective weights (penalties)
W_CA = 20  # per CA week away from 2
W_PH = 10  # per PH week above 1
W_SUBSTITUTE = 15  # per rounder replaced on a PH (more than W_PH, so teams stay together)
W_REQUEST = 10  # per 'OK' request not met
W_SPACING = 1  # per extra duty inside a 4-week window


@dataclass
class Team:
    ca: str | None
    rounds: list[str | None]  # R1..R6, the last being the R6 standby; None = blank slot


@dataclass
class Result:
    weeks: dict[dt.date, Team]  # keyed by the week's Friday
    ph: dict[dt.date, Team]  # keyed by the PH date
    status: str  # OPTIMAL, or FEASIBLE if the time limit stopped the search
    totals: dict[str, int]
    ca_count: dict[str, int]
    ph_count: dict[str, int]
    substitutions: int  # rounders replaced on a PH because they were on leave
    requests: tuple[int, int]  # (met, made)
    min_gap: int  # smallest number of weeks between anyone's duties


class _Progress(cp_model.CpSolverSolutionCallback):
    """Print each improved roster so a long solve never looks stuck."""

    def __init__(self, log):
        super().__init__()
        self.log = log

    def on_solution_callback(self):
        self.log(f"  {self.wall_time:5.0f}s  penalty {self.objective_value:.0f}")


def calendar(year: int, holidays: list[Holiday]) -> tuple[list[dt.date], dict[dt.date, dt.date]]:
    """Counted week Fridays, and each PH date -> the Friday of its week."""
    weeks = fridays(year)[1:]  # the first week is carried forward from last year
    ph = {h.observed: week_start(h.observed) for h in holidays if h.observed}
    return weeks, {d: f for d, f in sorted(ph.items()) if f in weeks}


def solve(
    year: int,
    holidays: list[Holiday],
    staff: list[Staff],
    leave: Leave,
    carried: set[str] = frozenset(),
    seconds: float = 180,
    log=print,
) -> Result | None:
    """Roster the year. `carried`: full names on duty in the week carried
    forward from last year, so their first duty this year is spaced from it."""
    weeks, ph = calendar(year, holidays)
    m = cp_model.CpModel()
    ca, rd, r6 = {}, {}, {}  # (name, friday) -> weekly role
    prd, pr6 = {}, {}  # (name, PH date) -> PH rounder / R6
    covers = {}  # (name, date) -> vars whose duty includes that date
    requests = []  # one 'met' flag per coverable 'OK' request

    def var(store, key, days):
        store[key] = v = m.new_bool_var("")
        for d in days:
            covers.setdefault((key[0], d), []).append(v)

    def get(store, key):
        return store.get(key, 0)

    for s in staff:
        p = s.full_name
        for f in weeks:
            whole_week = [f + i * DAY for i in range(7)]
            weekend = [f + DAY, f + 2 * DAY]
            if s.pame and leave.free(p, *whole_week):
                var(ca, (p, f), whole_week)
            if leave.free(p, *weekend):
                var(rd, (p, f), weekend)
                if s.group in R6_GROUPS:
                    var(r6, (p, f), weekend)
        for d in ph:
            if leave.free(p, d):
                var(prd, (p, d), [d])
                if s.group in R6_GROUPS:
                    var(pr6, (p, d), [d])

    # --- cover every slot ------------------------------------------------
    for f in weeks:
        m.add(sum(get(ca, (s.full_name, f)) for s in staff) == 1)
        m.add(sum(get(rd, (s.full_name, f)) for s in staff) == ROUNDERS)
        m.add(sum(get(r6, (s.full_name, f)) for s in staff) == 1)
    for d in ph:
        m.add(sum(get(prd, (s.full_name, d)) for s in staff) == ROUNDERS)
        m.add(sum(get(pr6, (s.full_name, d)) for s in staff) == 1)

    penalties, totals, ca_counts, ph_counts, busy_weeks = [], {}, {}, {}, {}
    for s in staff:
        p = s.full_name
        week_roles = {f: [get(ca, (p, f)), get(rd, (p, f)), get(r6, (p, f))] for f in weeks}

        # one duty per week; on a PH the week's team keeps its roles
        for roles in week_roles.values():
            m.add(sum(roles) <= 1)
        for d, f in ph.items():
            m.add(get(prd, (p, d)) + get(pr6, (p, d)) + get(ca, (p, f)) <= 1)
            if (p, d) in prd:  # free that day: rounders and R6 carry over
                m.add(prd[p, d] >= get(rd, (p, f)))
                # anyone on the PH who wasn't on that weekend is a substitute
                penalties.append(W_SUBSTITUTE * (prd[p, d] - get(rd, (p, f))))
                if (p, d) in pr6:
                    m.add(pr6[p, d] >= get(r6, (p, f)))
                    penalties.append(W_SUBSTITUTE * (pr6[p, d] - get(r6, (p, f))))

        # totals inside the group band
        ph_duties = {d: get(ca, (p, f)) + get(prd, (p, d)) + get(pr6, (p, d)) for d, f in ph.items()}
        total = sum(sum(r) for r in week_roles.values()) + sum(ph_duties.values())
        m.add(total >= s.tmin)
        m.add(total <= s.tmax)

        # CA: 1-3 weeks for PAME staff, 2 preferred
        n_ca = sum(get(ca, (p, f)) for f in weeks)
        if s.pame:
            m.add(n_ca >= CA_MIN)
            m.add(n_ca <= CA_MAX)
            off = m.new_int_var(0, CA_MAX, "")
            m.add(off >= n_ca - CA_AIM)
            m.add(off >= CA_AIM - n_ca)
            penalties.append(W_CA * off)

        # weekend R6 at most once (R6 inherited on a PH is not counted)
        m.add(sum(get(r6, (p, f)) for f in weeks) <= 1)

        # PHs: at most 2 days; preferably all in one week
        n_ph = sum(ph_duties.values())
        m.add(n_ph <= PH_MAX)
        ph_weeks = []
        for f in sorted(set(ph.values())):
            on = m.new_bool_var("")
            for d, w in ph.items():
                if w == f and not isinstance(ph_duties[d], int):
                    m.add(on >= ph_duties[d])
            ph_weeks.append(on)
        extra = m.new_int_var(0, PH_MAX, "")
        m.add(extra >= sum(ph_weeks) - PH_AIM)
        penalties.append(W_PH * extra)

        # 'OK' requests: penalise each one no duty covers
        for d in sorted(leave.requests[p]):
            if (p, d) in covers:
                met = m.new_bool_var("")
                m.add(met <= sum(covers[p, d]))
                penalties.append(W_REQUEST * (1 - met))
                requests.append(met)

        # spacing: extra duties inside any 4-week window, starting from the carried week
        busy = [int(p in carried)]
        for f in weeks:
            b = m.new_bool_var("")
            for x in week_roles[f] + [ph_duties[d] for d, w in ph.items() if w == f]:
                if not isinstance(x, int):
                    m.add(b >= x)
            busy.append(b)
        for i in range(len(busy) - SPACING_WEEKS + 1):
            crowd = m.new_int_var(0, SPACING_WEEKS, "")
            m.add(crowd >= sum(busy[i : i + SPACING_WEEKS]) - 1)
            penalties.append(W_SPACING * crowd)

        totals[p], ca_counts[p], ph_counts[p], busy_weeks[p] = total, n_ca, n_ph, busy

    m.minimize(sum(penalties))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_workers = 8
    status = solver.solve(m, _Progress(log))
    log(f"  {solver.status_name(status)} after {solver.wall_time:.0f}s")
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    # --- read the answer back ----------------------------------------------
    value = lambda x: x if isinstance(x, int) else solver.value(x)

    def chosen(store, key):
        return sorted(k[0] for k, v in store.items() if k[1] == key and solver.value(v))

    week_teams = {f: Team(chosen(ca, f)[0], chosen(rd, f) + chosen(r6, f)) for f in weeks}
    ph_teams = {}
    for d, f in ph.items():
        carried = week_teams[f].rounds[:ROUNDERS]
        on_ph = chosen(prd, d)
        subs = [p for p in on_ph if p not in carried]
        # carried-over rounders keep their slot; substitutes fill the gaps
        rounds = [p if p in on_ph else subs.pop(0) for p in carried]
        ph_teams[d] = Team(week_teams[f].ca, rounds + chosen(pr6, d))

    gaps = []
    for busy in busy_weeks.values():
        on = [i for i, b in enumerate(busy) if value(b)]
        gaps += [b - a for a, b in zip(on, on[1:])]

    return Result(
        weeks=week_teams,
        ph=ph_teams,
        status=solver.status_name(status),
        totals={p: value(t) for p, t in totals.items()},
        ca_count={p: value(n) for p, n in ca_counts.items()},
        ph_count={p: value(n) for p, n in ph_counts.items()},
        substitutions=sum(
            a != b for d, f in ph.items() for a, b in zip(week_teams[f].rounds, ph_teams[d].rounds)
        ),
        requests=(sum(solver.value(v) for v in requests), sum(len(r) for r in leave.requests.values())),
        min_gap=min(gaps, default=0),
    )
