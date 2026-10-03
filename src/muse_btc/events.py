"""BLS public release calendar and conservative CPI/payroll release extraction.

Official calendar: https://www.bls.gov/help/hlpiCAL.htm
Only explicitly published release times and numbers become actual observations.
"""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .intelligence import EvidenceRecord, IntelligenceStore
from .models import utc_now
from .providers.common import ProviderError
from .providers.public_intelligence import Page

CALENDAR_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
RELEASES = {
    "CPI": "https://www.bls.gov/news.release/cpi.htm",
    "NFP": "https://www.bls.gov/news.release/empsit.htm",
}


def calendar_entries(text):
    if "BEGIN:VCALENDAR" not in text:
        raise ProviderError("BLS calendar format changed")
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    result = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfolded, re.S):
        fields = {}
        for line in block.strip().splitlines():
            if ":" in line:
                key, value = line.strip().split(":", 1)
                fields[key] = value
        summary = fields.get("SUMMARY", "")
        if not any(
            s in summary.lower()
            for s in ("consumer price", "employment situation", "producer price")
        ):
            continue
        start = next(((k, v) for k, v in fields.items() if k.split(";")[0] == "DTSTART"), None)
        if not start or "RRULE" in fields:
            raise ProviderError("BLS event has no explicit supported release timestamp")
        key, value = start
        if value.endswith("Z"):
            at = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        else:
            tz = re.search(r"TZID=([^;]+)", key)
            if not tz or tz[1].strip('"') not in {"America/New_York", "US/Eastern"}:
                raise ProviderError("BLS event timezone missing or unsupported")
            at = (
                datetime.strptime(value, "%Y%m%dT%H%M%S")
                .replace(tzinfo=ZoneInfo("America/New_York"))
                .astimezone(UTC)
            )
        result.append(
            {
                "uid": fields.get("UID") or summary + at.isoformat(),
                "name": summary.replace(r"\,", ","),
                "release_time": at.isoformat(),
                "status": fields.get("STATUS", "SCHEDULED"),
                "source_url": CALENDAR_URL,
            }
        )
    if not result:
        raise ProviderError("No supported BLS calendar events; baseline not advanced")
    return result


def release_values(html, kind, received):
    text = " ".join(Page(html).text.split())
    match = re.search(
        r"(\d{1,2}:\d{2})\s+(a\.m\.|p\.m\.)\s+\(ET\)\s+\w+,\s+(\w+ \d{1,2}, \d{4})", text
    )
    if not match:
        raise ProviderError("BLS embargo timestamp missing")
    release = (
        datetime.strptime(
            match[3] + " " + match[1] + " " + ("AM" if match[2] == "a.m." else "PM"),
            "%B %d, %Y %I:%M %p",
        )
        .replace(tzinfo=ZoneInfo("America/New_York"))
        .astimezone(UTC)
    )
    if release > received:
        raise ProviderError("BLS actual data precedes its embargo time")
    rows = []
    if kind == "CPI":
        # Restrict parsing to the opening CPI-U statement, not arbitrary table numbers.
        m = re.search(
            r"Consumer Price Index for All Urban Consumers \(CPI-U\) "
            r"(increased|rose|decreased|fell) ([0-9.]+) percent on a seasonally adjusted basis",
            text,
        )
        if m:
            value = float(m[2]) * (-1 if m[1] in {"decreased", "fell"} else 1)
            rows.append(("CPI_MOM_SA", value, "percent", m[0]))
        elif re.search(
            r"Consumer Price Index for All Urban Consumers \(CPI-U\) "
            r"(was unchanged|remained unchanged) .*seasonally adjusted",
            text,
        ):
            rows.append(("CPI_MOM_SA", 0.0, "percent", "CPI-U explicitly unchanged"))
    elif kind == "NFP":
        opening = re.search(r"(?:Total|total) nonfarm payroll employment.{0,260}", text)
        if opening:
            m = re.search(r"(?:increased|rose|decreased|declined|fell) by ([\d,]+)", opening[0])
            signed = re.search(r"\(([+-][\d,]+)\)", opening[0])
            if m:
                negative = any(w in m[0] for w in ("decreased", "declined", "fell"))
                value = float(m[1].replace(",", "")) * (-1 if negative else 1)
                rows.append(("NFP_CHANGE", value, "persons", m[0]))
            elif signed:
                rows.append(("NFP_CHANGE", float(signed[1].replace(",", "")), "persons", signed[0]))
    if not rows:
        raise ProviderError("BLS release wording not recognized; no actual value inferred")
    return release, rows


class EventEngine:
    def __init__(self, store, settings, public):
        self.store, self.settings, self.public = store, settings, public
        self.archive = IntelligenceStore(store)

    async def collect(self):
        results = []
        token = self.archive.acquire("macro-events", utc_now(), 300)
        if not token:
            return {"status": "BUSY"}
        try:
            for kind, url in {"calendar": CALENDAR_URL, **RELEASES}.items():
                try:
                    html, raw, at = await self.public.fetch("BLS", url, {"www.bls.gov"})
                    if kind == "calendar":
                        items = calendar_entries(html)
                        for item in items:
                            self.archive.save(
                                EvidenceRecord(
                                    kind="macro_calendar",
                                    key=item["uid"],
                                    source="BLS",
                                    market_time=at,
                                    available_at=at,
                                    raw_ids=[raw],
                                    data=item,
                                )
                            )
                    else:
                        release, items = release_values(html, kind, at)
                        for name, value, units, excerpt in items:
                            self.archive.save(
                                EvidenceRecord(
                                    kind="macro_event",
                                    key=f"BLS:{name}:{release.isoformat()}",
                                    source="BLS",
                                    market_time=release,
                                    available_at=at,
                                    raw_ids=[raw],
                                    data={
                                        "name": name,
                                        "release_time": release.isoformat(),
                                        "actual": value,
                                        "consensus": None,
                                        "previous": None,
                                        "surprise": None,
                                        "units": units,
                                        "vintage": "PUBLIC_RELEASE_RECEIVED_NOW",
                                        "source_url": url,
                                        "evidence_excerpt": excerpt,
                                        "consensus_status": "UNAVAILABLE",
                                    },
                                )
                            )
                    results.append({"source": kind, "status": "ARCHIVED", "items": len(items)})
                except (ProviderError, ValueError, KeyError) as exc:
                    results.append({"source": kind, "status": "UNAVAILABLE", "reason": str(exc)})
            self.store.set_state(
                "event_checks", {"checked_at": utc_now().isoformat(), "sources": results}
            )
            return {
                "status": "COMPLETE"
                if all(r["status"] == "ARCHIVED" for r in results)
                else "DEGRADED",
                "sources": results,
            }
        finally:
            self.archive.release("macro-events", token)

    def calendar(self, now):
        rows = self.archive.records("macro_calendar", now)
        return sorted(
            [
                r.model_dump(mode="json")
                for r in rows
                if datetime.fromisoformat(r.data["release_time"]) >= now - timedelta(days=1)
            ],
            key=lambda r: r["data"]["release_time"],
        )
