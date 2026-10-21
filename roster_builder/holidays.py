"""Singapore public holidays, and how each one lands on the roster.

Roster rules:
  weekday PH -> gets its own roster row, inheriting the team of the Fri-Thu
                week it falls in
  Sunday PH  -> the Monday in lieu gets the row; the Sunday itself is left as a
                plain date on the weekend row
  Saturday PH-> no row and no day in lieu; it is absorbed into that weekend,
                annotated on the week row's Saturday cell
"""

from __future__ import annotations

import datetime as dt
import json
import ssl
import urllib.request
from dataclasses import dataclass

# Actual (gazetted) dates only - in-lieu days are derived, never stored here.
# Used when offline or when MOM has not yet published the year.
FALLBACK: dict[int, list[tuple[str, int, int]]] = {
    2026: [
        ("New Year's Day", 1, 1),
        ("Chinese New Year", 2, 17),
        ("Chinese New Year", 2, 18),
        ("Hari Raya Puasa", 3, 21),
        ("Good Friday", 4, 3),
        ("Labour Day", 5, 1),
        ("Hari Raya Haji", 5, 27),
        ("Vesak Day", 5, 31),
        ("National Day", 8, 9),
        ("Deepavali", 11, 8),
        ("Christmas Day", 12, 25),
    ],
}

# MOM publishes one data.gov.sg dataset per year inside this collection.
COLLECTION_URL = "https://api-production.data.gov.sg/v2/public/api/collections/691/metadata"
DATASET_URL = "https://data.gov.sg/api/action/datastore_search?resource_id={}&limit=100"


@dataclass(frozen=True)
class Holiday:
    """One gazetted public holiday."""

    name: str
    actual: dt.date

    @property
    def absorbed(self) -> bool:
        """True if it falls on a Saturday: no roster row, no day in lieu."""
        return self.actual.weekday() == 5

    @property
    def observed(self) -> dt.date | None:
        """The day the roster rosters. None when absorbed into a weekend."""
        if self.absorbed:
            return None
        return self.actual + dt.timedelta(days=self.actual.weekday() == 6)  # Sun -> Mon

    @property
    def treatment(self) -> str:
        if self.absorbed:
            return "absorbed into weekend (no row)"
        return "Monday in lieu" if self.observed != self.actual else "own row"

    def label(self) -> str:
        """Roster label, e.g. 'VESAK DAY (MON)'."""
        return f"{self.name} ({(self.observed or self.actual):%a})".upper()

    def saturday_label(self) -> str:
        """Annotation written onto the weekend row, e.g. '21-Mar-26 HARI RAYA PUASA (SAT)'."""
        return f"{self.actual:%d-%b-%y} {self.name.upper()} (SAT)"


def from_fallback(year: int) -> list[Holiday]:
    if year not in FALLBACK:
        raise KeyError(f"no built-in holiday list for {year} - fetch or enter manually")
    return [Holiday(n, dt.date(year, m, d)) for n, m, d in FALLBACK[year]]


def download(url: str) -> bytes:
    """Fetch a URL. Uses certifi's certificates when available: python.org's
    macOS Python has none of its own, so HTTPS fails without them."""
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = None
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})  # default UA gets 403
    with urllib.request.urlopen(req, timeout=15, context=context) as resp:
        return resp.read()


def _get(url: str) -> dict:
    return json.loads(download(url))


def fetch(year: int) -> list[Holiday]:
    """MOM's list for `year` from data.gov.sg, else the built-in list.

    Hari Raya dates are provisional until gazetted, so the list is always
    presented for confirmation.
    """
    try:
        ids = _get(COLLECTION_URL)["data"]["collectionMetadata"]["childDatasets"]
        for ds in reversed(ids):  # newest years are listed last
            records = _get(DATASET_URL.format(ds))["result"]["records"]
            if records and records[0]["date"].startswith(str(year)):
                return [
                    Holiday(r["holiday"].replace("’", "'").strip(), dt.date.fromisoformat(r["date"]))
                    for r in records
                    if "observed" not in r["holiday"].lower()  # in-lieu days are derived
                ]
        print(f"  MOM has not published {year} yet, using built-in list")
    except Exception as exc:  # offline, rate-limited, schema change
        print(f"  holiday fetch failed ({exc.__class__.__name__}), using built-in list")
    return from_fallback(year)


def span_extra(year: int) -> list[Holiday]:
    """Holidays from the following year that fall inside this roster's span.

    The last week row starts on the last Friday on or before 1 Jan of year+1,
    so the span reaches at most 7 Jan of year+1. New Year's Day is the only
    Singapore public holiday that can ever fall in 1-7 January, and its date is
    fixed, so this needs no fetch and cannot be provisional.
    """
    return [Holiday(f"New Year {year + 1}", dt.date(year + 1, 1, 1))]
