#!/usr/bin/env python3
"""Free daily PSX digest: KSE-100 trend + Pakistan business headlines.

Uses only the Python standard library and public web sources (PSX data portal
end-of-day series, Yahoo Finance as fallback, Dawn / Business Recorder /
Tribune RSS). Personal use only. Not financial advice.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
PKT = timezone(timedelta(hours=5))
REPORTS_DIR = Path("reports")

PSX_EOD_URL = "https://dps.psx.com.pk/timeseries/eod/KSE100"
PSX_INTRADAY_URL = "https://dps.psx.com.pk/timeseries/int/KSE100"

# Yahoo delayed quotes as a fallback if the PSX portal is unreachable.
YAHOO_SYMBOLS = ("KSE100.KA", "KSE100.PSX", "^KSE100")
YAHOO_ENDPOINTS = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
    "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
)

NEWS_FEEDS = (
    ("Dawn Business", "https://www.dawn.com/feeds/business"),
    ("Business Recorder", "https://www.brecorder.com/feeds/latest-news"),
    ("Express Tribune", "https://tribune.com.pk/feed/business"),
)

MARKET_WORDS = (
    "psx",
    "kse",
    "stock",
    "share",
    "index",
    "rupee",
    "sbp",
    "secp",
    "equity",
    "ipo",
    "dividend",
    "market",
    "bursa",
    "listed",
    "cement",
    "bank",
    "oil",
    "gas",
    "fertilizer",
    "policy rate",
    "t-bill",
    "imf",
)


@dataclass
class QuoteSeries:
    symbol: str
    closes: list[tuple[date, float]]
    live: tuple[date, float] | None = None
    source: str = ""

    @property
    def last(self) -> tuple[date, float]:
        return self.live or self.closes[-1]


@dataclass
class Headline:
    source: str
    title: str
    link: str
    published: str


def http_get(url: str, timeout: int = 25, referer: str | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/xml, */*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_psx_eod() -> QuoteSeries:
    payload = json.loads(http_get(PSX_EOD_URL, referer="https://dps.psx.com.pk/"))
    if payload.get("status") != 1 or not payload.get("data"):
        raise RuntimeError(f"PSX EOD feed returned {payload.get('message') or payload}")
    pairs: list[tuple[date, float]] = []
    for row in payload["data"]:
        if not row or row[1] is None:
            continue
        day = datetime.fromtimestamp(int(row[0]), tz=PKT).date()
        pairs.append((day, float(row[1])))
    pairs.sort(key=lambda item: item[0])
    # Keep the last close per calendar day in case a session is duplicated.
    by_day: dict[date, float] = {day: close for day, close in pairs}
    ordered = sorted(by_day.items())
    if len(ordered) < 5:
        raise RuntimeError("PSX EOD feed did not include enough KSE-100 sessions")
    live = fetch_psx_intraday(ordered[-1][0])
    return QuoteSeries(symbol="KSE100", closes=ordered, live=live, source="PSX data portal")


def fetch_psx_intraday(last_eod_day: date) -> tuple[date, float] | None:
    """If the market is open, overlay the latest tick on top of yesterday's close."""
    try:
        payload = json.loads(http_get(PSX_INTRADAY_URL, referer="https://dps.psx.com.pk/"))
        rows = payload.get("data") or []
        if not rows:
            return None
        newest = rows[0]
        day = datetime.fromtimestamp(int(newest[0]), tz=PKT).date()
        price = float(newest[1])
        if day > last_eod_day and price > 0:
            return day, price
    except Exception as exc:  # noqa: BLE001 - live overlay is optional
        print(f"Intraday overlay skipped: {exc}", file=sys.stderr)
    return None


def fetch_yahoo_series(symbol: str) -> QuoteSeries:
    last_error: Exception | None = None
    encoded = urllib.parse.quote(symbol, safe=".")
    query = "range=1y&interval=1d&includePrePost=false"
    for attempt in range(4):
        for base in YAHOO_ENDPOINTS:
            url = f"{base.format(symbol=encoded)}?{query}"
            try:
                payload = json.loads(http_get(url))
                result = (payload.get("chart") or {}).get("result") or []
                if not result:
                    error = (payload.get("chart") or {}).get("error")
                    raise RuntimeError(f"Yahoo returned no series for {symbol}: {error}")
                timestamps = result[0].get("timestamp") or []
                closes = (
                    ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close")
                    or []
                )
                pairs: list[tuple[date, float]] = []
                for ts, close in zip(timestamps, closes):
                    if ts is None or close is None:
                        continue
                    day = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
                    pairs.append((day, float(close)))
                if len(pairs) < 5:
                    raise RuntimeError(f"Not enough daily closes for {symbol}")
                return QuoteSeries(symbol=symbol, closes=pairs, source="Yahoo Finance")
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
                last_error = exc
                # Yahoo rate-limits with 429; back off and try the next host.
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    time.sleep(2 ** attempt)
        time.sleep(1)
    raise RuntimeError(f"Could not fetch {symbol} from Yahoo Finance: {last_error}")


def load_kse100() -> QuoteSeries:
    errors: list[str] = []
    try:
        return fetch_psx_eod()
    except Exception as exc:  # noqa: BLE001 - fall back to Yahoo
        errors.append(f"PSX: {exc}")
    for symbol in YAHOO_SYMBOLS:
        try:
            return fetch_yahoo_series(symbol)
        except Exception as exc:  # noqa: BLE001 - try the next ticker
            errors.append(f"{symbol}: {exc}")
    raise RuntimeError("KSE-100 data unavailable. " + " | ".join(errors))


def moving_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def pct(change: float, base: float) -> float:
    if base == 0:
        return 0.0
    return (change / base) * 100


def classify_regime(price: float, ma50: float | None, ma200: float | None) -> tuple[str, str, str]:
    """Return (label, stance, why). Conservative, not a buy/sell call."""
    if ma200 is None:
        return (
            "INSUFFICIENT HISTORY",
            "Sit tight until a longer series is available.",
            "Need about 200 trading days before a trend-vs-200-day-average read is meaningful.",
        )

    above_200 = price > ma200
    if ma50 is not None and ma50 > ma200 and above_200:
        return (
            "UPTREND (price and 50-day average above 200-day average)",
            "Trend is supportive of long-horizon accumulation, not a green light to lump-sum.",
            "Price is above the 200-day average and the 50-day average is also above it. "
            "That is a rising-market regime. One green day still does not make a buy call.",
        )
    if ma50 is not None and ma50 < ma200 and not above_200:
        return (
            "DOWNTREND (price and 50-day average below 200-day average)",
            "Sit tight or dollar-cost average only with money you can leave untouched.",
            "Price is below the 200-day average and the 50-day average is also below it. "
            "That is a weakening-market regime. Catching a falling knife is how capital gets lost.",
        )
    if above_200:
        return (
            "MIXED / LATE TREND (above 200-day, not confirmed by 50-day)",
            "Do not chase. If you invest, keep it small and staggered.",
            "The long average is still underneath price, but shorter trend is not clean. "
            "Whipsaws are common in this zone.",
        )
    return (
        "MIXED / WEAK (below 200-day, not a clean downtrend)",
        "Prefer waiting or tiny staged buys over a lump sum.",
        "Price is under the 200-day average. Until it reclaims that line and holds, "
        "the burden of proof is on the bulls.",
    )


def looks_market_related(title: str) -> bool:
    lowered = title.lower()
    return any(word in lowered for word in MARKET_WORDS)


def local_text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return " ".join(node.text.split())


def parse_rss(source: str, xml_bytes: bytes) -> list[Headline]:
    root = ET.fromstring(xml_bytes)
    headlines: list[Headline] = []
    for item in root.findall("./channel/item"):
        title = local_text(item.find("title"))
        link = local_text(item.find("link"))
        published = local_text(item.find("pubDate"))
        if not title:
            continue
        headlines.append(Headline(source=source, title=title, link=link, published=published))
    return headlines


def fetch_headlines(limit: int = 8) -> list[Headline]:
    buckets: list[list[Headline]] = []
    for source, url in NEWS_FEEDS:
        try:
            items = parse_rss(source, http_get(url))
        except Exception as exc:  # noqa: BLE001 - skip a dead feed
            print(f"News feed skipped ({source}): {exc}", file=sys.stderr)
            continue
        related = [item for item in items if looks_market_related(item.title)]
        buckets.append(related or items[:4])

    collected: list[Headline] = []
    seen: set[str] = set()
    index = 0
    while len(collected) < limit and buckets:
        progressed = False
        for bucket in buckets:
            if index >= len(bucket):
                continue
            item = bucket[index]
            key = item.title.casefold()
            progressed = True
            if key in seen:
                continue
            seen.add(key)
            collected.append(item)
            if len(collected) >= limit:
                break
        if not progressed:
            break
        index += 1
    return collected


def fmt(n: float, digits: int = 2) -> str:
    return f"{n:,.{digits}f}"


def build_report(series: QuoteSeries, headlines: Iterable[Headline]) -> str:
    closes = [close for _, close in series.closes]
    last_day, last_close = series.last
    eod_day, eod_close = series.closes[-1]
    baseline = eod_close if last_day != eod_day else closes[-2]
    prev_close = baseline
    day_change = last_close - prev_close
    day_pct = pct(day_change, prev_close)
    five_pct = pct(last_close - closes[-6], closes[-6]) if len(closes) >= 6 else None
    ma20 = moving_average(closes, 20)
    ma50 = moving_average(closes, 50)
    ma200 = moving_average(closes, 200)
    day_label = "UP" if day_change > 0 else "DOWN" if day_change < 0 else "FLAT"
    regime, stance, why = classify_regime(last_close, ma50, ma200)
    now_pkt = datetime.now(PKT).strftime("%Y-%m-%d %H:%M PKT")

    vs_200 = ""
    if ma200 is not None:
        vs_200 = f"{pct(last_close - ma200, ma200):+.2f}% vs 200-day average"

    news_lines = []
    for item in headlines:
        extra = f" ({item.published})" if item.published else ""
        if item.link:
            news_lines.append(f"- **{item.source}:** [{item.title}]({item.link}){extra}")
        else:
            news_lines.append(f"- **{item.source}:** {item.title}{extra}")
    news_block = "\n".join(news_lines) if news_lines else "- No headlines could be fetched today."

    return f"""# Daily PSX digest

Generated: {now_pkt}
Source: {series.source or "public delayed quote"} (`{series.symbol}`), last point {last_day.isoformat()}
This is **not financial advice**. You can lose money in equities. One day is noise. For personal use only.

## Market direction

| | |
| --- | --- |
| KSE-100 | {fmt(last_close)} |
| Day | {day_label} {day_change:+,.2f} ({day_pct:+.2f}%) |
| 5 sessions | {f"{five_pct:+.2f}%" if five_pct is not None else "n/a"} |
| 20-day average | {fmt(ma20) if ma20 is not None else "n/a"} |
| 50-day average | {fmt(ma50) if ma50 is not None else "n/a"} |
| 200-day average | {fmt(ma200) if ma200 is not None else "n/a"} |
| vs 200-day | {vs_200 or "n/a"} |

**Regime:** {regime}

**Stance:** {stance}

{why}

Short-term volatility (last 20 closes, sample stdev): {fmt(statistics.pstdev(closes[-20:]), 1) if len(closes) >= 20 else "n/a"} index points.

## News (today's public headlines)

{news_block}

## How to read this

- Green today ≠ good time to invest. Compare price to the 200-day average first.
- Uptrend still does not mean buy everything. Size positions you can hold through a 20%+ drawdown.
- If you cannot explain why you own a name for 3+ years, skip it.
"""


def post_webhook(text: str) -> None:
    url = os.environ.get("DIGEST_WEBHOOK_URL", "").strip()
    if not url:
        return
    # Discord wants {"content": ...}; Slack incoming webhooks want {"text": ...}.
    payload = {"content": text[:1900]} if "discord.com" in url else {"text": text[:3900]}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def write_reports(markdown: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamped = REPORTS_DIR / f"{date.today().isoformat()}.md"
    latest = REPORTS_DIR / "latest.md"
    stamped.write_text(markdown, encoding="utf-8")
    latest.write_text(markdown, encoding="utf-8")
    return stamped


def append_github_summary(markdown: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(summary, "a", encoding="utf-8") as handle:
        handle.write(markdown)
        handle.write("\n")


def main() -> int:
    try:
        series = load_kse100()
        headlines = fetch_headlines()
        report = build_report(series, headlines)
    except Exception as exc:  # noqa: BLE001 - surface a readable job failure
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(report)
    path = write_reports(report)
    append_github_summary(report)
    try:
        post_webhook(report)
    except Exception as exc:  # noqa: BLE001 - digest still succeeded
        print(f"Webhook skipped: {exc}", file=sys.stderr)
    print(f"\nWrote {path} and {REPORTS_DIR / 'latest.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
