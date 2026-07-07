"""Dark pool / off-exchange activity from FINRA's free public datasets.

Two sources, both free and unauthenticated:

- **Reg SHO daily short-sale volume** (consolidated NMS file on FINRA's CDN,
  pipe-delimited text) — per-ticker short volume vs total off-exchange
  volume, published nightly.
- **ATS weekly summary** (FINRA OTC Transparency API) — per-ticker share
  volume executed on alternative trading systems ("dark pools"), published
  weekly with a lag.

Commercial platforms overlay per-print tape data (licensed); these public
aggregates cover the same question — how much of a name is trading off
exchange and how short-biased is it — at daily/weekly resolution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from src.data.http_util import request_with_retry

logger = logging.getLogger(__name__)

REGSHO_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{yyyymmdd}.txt"
ATS_URL = "https://api.finra.org/data/group/otcMarket/name/weeklySummary"


@dataclass
class ShortVolumeDay:
    ticker: str
    day: date
    short_volume: int
    total_volume: int

    @property
    def short_ratio(self) -> float:
        return self.short_volume / self.total_volume if self.total_volume else 0.0


def parse_regsho_text(text: str) -> dict[str, ShortVolumeDay]:
    """Parse a Reg SHO daily file into {ticker: ShortVolumeDay}.

    Format: ``Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market``
    with a header line and a trailing record-count line.
    """
    result: dict[str, ShortVolumeDay] = {}
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 5 or parts[0] == "Date":
            continue
        try:
            day = date(int(parts[0][:4]), int(parts[0][4:6]), int(parts[0][6:8]))
            result[parts[1].upper()] = ShortVolumeDay(
                ticker=parts[1].upper(),
                day=day,
                short_volume=int(parts[2]),
                total_volume=int(parts[4]),
            )
        except (ValueError, IndexError):
            continue
    return result


class DarkPoolService:
    async def get_short_volume(
        self, ticker: str, *, days: int = 5
    ) -> list[ShortVolumeDay]:
        """Recent daily short-volume records for *ticker*, oldest first.

        Walks back from today skipping weekends/holidays (missing files).
        """
        ticker = ticker.upper()
        records: list[ShortVolumeDay] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            day = date.today()
            attempts = 0
            while len(records) < days and attempts < days * 3:
                attempts += 1
                day -= timedelta(days=1)
                if day.weekday() >= 5:
                    continue
                url = REGSHO_URL.format(yyyymmdd=day.strftime("%Y%m%d"))
                try:
                    resp = await request_with_retry(client, "GET", url, attempts=1)
                    if resp.status_code != 200:
                        continue
                    parsed = parse_regsho_text(resp.text)
                except Exception:
                    logger.debug("DarkPoolService: no Reg SHO file for %s", day)
                    continue
                if ticker in parsed:
                    records.append(parsed[ticker])
        records.sort(key=lambda r: r.day)
        return records

    async def get_ats_weekly(self, ticker: str, *, weeks: int = 4) -> list[dict]:
        """Weekly ATS (dark pool) share volume for *ticker*, newest first.

        Best-effort: the FINRA API occasionally requires registration for
        burst traffic; failures return [] and the caller degrades gracefully.
        """
        body = {
            "limit": weeks * 2,
            "compareFilters": [
                {
                    "compareType": "EQUAL",
                    "fieldName": "issueSymbolIdentifier",
                    "fieldValue": ticker.upper(),
                },
                {
                    "compareType": "EQUAL",
                    "fieldName": "summaryTypeCode",
                    "fieldValue": "ATS_W_SMBL",
                },
            ],
            "sortFields": ["-weekStartDate"],
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await request_with_retry(
                    client,
                    "POST",
                    ATS_URL,
                    json=body,
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                rows = resp.json()
        except Exception:
            logger.warning(
                "DarkPoolService: ATS weekly data unavailable for %s", ticker, exc_info=True
            )
            return []

        out = []
        for row in rows if isinstance(rows, list) else []:
            out.append(
                {
                    "week": row.get("weekStartDate", ""),
                    "shares": int(row.get("totalWeeklyShareQuantity") or 0),
                    "trades": int(row.get("totalWeeklyTradeCount") or 0),
                }
            )
        return out[:weeks]
