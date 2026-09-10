#!/usr/bin/env python3
"""Free daily PSX digest: sentiment, playbook, and historically strong names.

Uses only the Python standard library and public PSX / news sources.
Personal use only. Not financial advice.
"""

from __future__ import annotations

import html as html_lib
import http.cookiejar
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
PKT = timezone(timedelta(hours=5))
REPORTS_DIR = Path("reports")
PSX_ORIGIN = "https://dps.psx.com.pk"

NEWS_FEEDS = (
    ("Dawn Business", "https://www.dawn.com/feeds/business"),
    ("Business Recorder", "https://www.brecorder.com/feeds/latest-news"),
    ("Express Tribune", "https://tribune.com.pk/feed/business"),
)

MARKET_WORDS = (
    "psx", "kse", "stock", "share", "index", "rupee", "sbp", "secp", "equity",
    "ipo", "dividend", "market", "listed", "cement", "bank", "oil", "gas",
    "fertilizer", "policy rate", "t-bill", "imf", "profit", "loss", "rally",
)

POS_WORDS = (
    "surge", "rally", "gain", "gains", "record", "bull", "upgrade", "inflow",
    "inflows", "profit", "recovery", "rebound", "jump", "rise", "rises",
    "optimistic", "strong", "growth",
)
NEG_WORDS = (
    "fall", "falls", "dip", "drop", "crash", "bear", "loss", "losses", "sell-off",
    "selloff", "outflow", "outflows", "tension", "uncertainty", "weak", "slide",
    "decline", "fear", "crisis", "default", "downgrade",
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
    tone: str = "neutral"


@dataclass
class Quote:
    symbol: str
    name: str
    sector: str
    listed: str
    current: float
    change: float
    pct: float
    volume: float
    weight: float | None = None
    mcap: float | None = None


@dataclass
class SectorRow:
    code: str
    name: str
    advance: int
    decline: int
    unchanged: int
    turnover: float
    mcap: float


@dataclass
class StockHistory:
    symbol: str
    name: str
    sector: str
    last: float
    day_pct: float
    r15d: float | None
    r1m: float | None
    r3m: float | None
    r6m: float | None
    r1y: float | None
    vs_15: float | None
    vs_50: float | None
    vs_200: float | None
    above_200: bool
    volume: float = 0.0
    mcap: float | None = None


@dataclass
class Digest:
    generated: str
    index: QuoteSeries
    kse30: QuoteSeries | None
    allshr: QuoteSeries | None
    quotes: list[Quote]
    kse100: list[Quote]
    sectors: list[SectorRow]
    histories: list[StockHistory]
    headlines: list[Headline]
    day_label: str
    day_change: float
    day_pct: float
    five_pct: float | None
    fifteen_pct: float | None
    ma15: float | None
    ma20: float | None
    ma50: float | None
    ma200: float | None
    regime: str
    stance: str
    why: str
    sentiment_score: int
    sentiment_label: str
    sentiment_detail: list[str]
    playbook: list[str]
    invest_today: list[str]
    watchlist: list[StockHistory]
    vs_yesterday: list[str]
    lesson: str
    closed_note: str


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] = []
        self._row: list[str] = []
        self._cell: list[str] = []
        self._in_table = self._in_tr = self._in_cell = self._in_thead = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._in_table = True
            self._table = []
        elif tag == "thead":
            self._in_thead = True
        elif tag == "tr" and self._in_table:
            self._in_tr = True
            self._row = []
        elif tag in ("td", "th") and self._in_tr:
            self._in_cell = True
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._row.append(html_lib.unescape("".join(self._cell)).strip())
        elif tag == "tr" and self._in_tr:
            self._in_tr = False
            if self._row and not self._in_thead:
                self._table.append(self._row)
        elif tag == "thead":
            self._in_thead = False
        elif tag == "table" and self._in_table:
            self._in_table = False
            self.tables.append(self._table)

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell.append(data)


_COOKIE_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIE_JAR))


def http_get_curl(url: str, referer: str | None, timeout: int) -> bytes:
    """Force IPv4. GitHub-hosted runners often stall or get RST on IPv6 to PSX."""
    cmd = [
        "curl",
        "-4",
        "-sS",
        "-L",
        "--compressed",
        "--fail",
        "--max-time",
        str(timeout),
        "-A",
        USER_AGENT,
        "-H",
        "Accept: application/json, text/html, text/xml, */*;q=0.8",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
    ]
    if referer:
        cmd.extend(["-e", referer])
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 8)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")[:400]
        raise RuntimeError(f"curl {proc.returncode}: {err or 'no stderr'}")
    if not proc.stdout:
        raise RuntimeError("curl returned empty body")
    return proc.stdout


def http_get_urllib(url: str, referer: str | None, timeout: int) -> bytes:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/html, text/xml, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.read()


def http_get(url: str, timeout: int = 40, referer: str | None = None, attempts: int = 2) -> bytes:
    last: Exception | None = None
    methods: list[tuple[str, object]] = []
    if shutil.which("curl"):
        methods.append(("curl", lambda: http_get_curl(url, referer, timeout)))
    methods.append(("urllib", lambda: http_get_urllib(url, referer, timeout)))
    for attempt in range(max(1, attempts)):
        for name, fn in methods:
            try:
                body = fn()  # type: ignore[operator]
                if body:
                    return body
            except Exception as exc:  # noqa: BLE001
                last = exc
                print(f"GET {url} via {name} failed ({exc})", file=sys.stderr)
        time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def psx_get(path: str) -> bytes:
    return http_get(f"{PSX_ORIGIN}{path}", referer=f"{PSX_ORIGIN}/")


def warmup_psx() -> None:
    try:
        http_get(f"{PSX_ORIGIN}/")
    except Exception as exc:  # noqa: BLE001
        print(f"PSX homepage warmup skipped: {exc}", file=sys.stderr)


def parse_tables(raw: bytes | str) -> list[list[list[str]]]:
    parser = TableParser()
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    parser.feed(text)
    return parser.tables


def num(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = value.replace(",", "").replace("%", "").replace("—", "").strip()
    if cleaned in {"", "-", "n/a", "N/A"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def pct(change: float, base: float) -> float:
    if base == 0:
        return 0.0
    return (change / base) * 100


def moving_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def ret(values: list[float], sessions: int) -> float | None:
    if len(values) < sessions + 1:
        return None
    return pct(values[-1] - values[-1 - sessions], values[-1 - sessions])


def series_values(series: QuoteSeries) -> list[float]:
    closes = [c for _, c in series.closes]
    last_day, last_px = series.last
    if last_day != series.closes[-1][0]:
        return closes + [last_px]
    return closes


def series_window(
    series: QuoteSeries | None, sessions: int
) -> tuple[float | None, float | None, float | None]:
    """Return over N sessions, N-day average, and % vs that average."""
    if series is None or not series.closes:
        return None, None, None
    values = series_values(series)
    last_px = series.last[1]
    r = ret(values, sessions)
    ma = moving_average(values, sessions)
    vs = None if ma is None else pct(last_px - ma, ma)
    return r, ma, vs


def fmt(n: float | None, digits: int = 2) -> str:
    if n is None:
        return "n/a"
    return f"{n:,.{digits}f}"


def fmt_pct(n: float | None) -> str:
    if n is None:
        return "n/a"
    return f"{n:+.2f}%"


def signed(n: float) -> str:
    return f"{n:+,.2f}"


def parse_eod_payload(symbol: str, payload: dict, source: str) -> QuoteSeries:
    if payload.get("status") != 1 or not payload.get("data"):
        raise RuntimeError(f"{symbol} EOD feed returned {payload.get('message') or 'no data'}")
    by_day: dict[date, float] = {}
    for row in payload["data"]:
        if not row or row[1] is None:
            continue
        day = datetime.fromtimestamp(int(row[0]), tz=PKT).date()
        by_day[day] = float(row[1])
    ordered = sorted(by_day.items())
    if len(ordered) < 5:
        raise RuntimeError(f"Not enough daily closes for {symbol}")
    return QuoteSeries(symbol=symbol, closes=ordered, source=source)


def fetch_psx_eod(symbol: str) -> QuoteSeries:
    payload = json.loads(psx_get(f"/timeseries/eod/{urllib.parse.quote(symbol)}"))
    return parse_eod_payload(symbol, payload, "PSX data portal")


def fetch_psx_intraday(symbol: str, last_eod_day: date) -> tuple[date, float] | None:
    try:
        payload = json.loads(psx_get(f"/timeseries/int/{urllib.parse.quote(symbol)}"))
        rows = payload.get("data") or []
        if not rows:
            return None
        newest = rows[0]
        day = datetime.fromtimestamp(int(newest[0]), tz=PKT).date()
        price = float(newest[1])
        if day > last_eod_day and price > 0:
            return day, price
    except Exception as exc:  # noqa: BLE001
        print(f"Intraday overlay skipped ({symbol}): {exc}", file=sys.stderr)
    return None


def fetch_yahoo_series(symbol: str) -> QuoteSeries:
    last_error: Exception | None = None
    encoded = urllib.parse.quote(symbol, safe=".")
    query = "range=1y&interval=1d&includePrePost=false"
    hosts = (
        "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
    )
    for attempt in range(4):
        for base in hosts:
            try:
                payload = json.loads(http_get(f"{base.format(symbol=encoded)}?{query}"))
                result = (payload.get("chart") or {}).get("result") or []
                if not result:
                    raise RuntimeError((payload.get("chart") or {}).get("error"))
                timestamps = result[0].get("timestamp") or []
                closes = (((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
                pairs: list[tuple[date, float]] = []
                for ts, close in zip(timestamps, closes):
                    if ts is None or close is None:
                        continue
                    pairs.append((datetime.fromtimestamp(int(ts), tz=timezone.utc).date(), float(close)))
                if len(pairs) < 5:
                    raise RuntimeError(f"Not enough daily closes for {symbol}")
                return QuoteSeries(symbol=symbol, closes=pairs, source="Yahoo Finance")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    time.sleep(2 ** attempt)
        time.sleep(1)
    raise RuntimeError(f"Yahoo failed for {symbol}: {last_error}")


def fetch_index_snapshot(symbol: str) -> QuoteSeries | None:
    """Last-resort: current vs previous from the trading panel HTML."""
    try:
        tables = parse_tables(psx_get("/trading-panel"))
    except Exception as exc:  # noqa: BLE001
        print(f"Trading panel snapshot skipped: {exc}", file=sys.stderr)
        return None
    wanted = symbol.upper()
    for table in tables:
        for row in table:
            if not row or row[0].replace(" ", "").upper() != wanted:
                continue
            current = num(row[1]) if len(row) > 1 else None
            change = None
            for cell in row[2:]:
                parsed = num(cell)
                if parsed is None:
                    continue
                # Prefer an explicit change column (small vs index level).
                if current is not None and abs(parsed) < abs(current) * 0.2:
                    change = parsed
                    break
            if current is None:
                continue
            if change is None:
                change = 0.0
            today = datetime.now(PKT).date()
            prev = current - change
            return QuoteSeries(
                symbol=symbol,
                closes=[(today - timedelta(days=1), prev), (today, current)],
                source="PSX trading panel snapshot",
            )
    return None


def load_index(symbol: str) -> QuoteSeries:
    try:
        series = fetch_psx_eod(symbol)
        series.live = fetch_psx_intraday(symbol, series.closes[-1][0])
        return series
    except Exception as exc:
        print(f"{symbol} PSX fetch failed ({exc}); trying Yahoo", file=sys.stderr)
        if symbol == "KSE100":
            for yahoo in ("KSE100.KA", "KSE100.PSX", "^KSE100"):
                try:
                    return fetch_yahoo_series(yahoo)
                except Exception as yahoo_exc:  # noqa: BLE001
                    print(f"Yahoo {yahoo} failed ({yahoo_exc})", file=sys.stderr)
        snapshot = fetch_index_snapshot(symbol)
        if snapshot:
            print(f"{symbol} using trading-panel snapshot fallback", file=sys.stderr)
            return snapshot
        raise


def load_optional_index(symbol: str) -> QuoteSeries | None:
    try:
        return load_index(symbol)
    except Exception as exc:  # noqa: BLE001
        print(f"Optional index {symbol} skipped: {exc}", file=sys.stderr)
        return None


def fetch_market_watch() -> list[Quote]:
    tables = parse_tables(psx_get("/market-watch"))
    rows = tables[0] if tables else []
    quotes: list[Quote] = []
    for row in rows:
        if len(row) < 11:
            continue
        current, change, percent, volume = num(row[7]), num(row[8]), num(row[9]), num(row[10])
        if current is None or percent is None:
            continue
        quotes.append(
            Quote(
                symbol=row[0],
                name=row[0],
                sector=row[1],
                listed=row[2],
                current=current,
                change=change or 0.0,
                pct=percent,
                volume=volume or 0.0,
            )
        )
    return quotes


def fetch_kse100_board() -> list[Quote]:
    tables = parse_tables(psx_get("/indices/KSE100"))
    rows = tables[0] if tables else []
    out: list[Quote] = []
    for row in rows:
        if len(row) < 8:
            continue
        current, change, percent, weight, volume = num(row[3]), num(row[4]), num(row[5]), num(row[6]), num(row[8])
        if current is None or percent is None:
            continue
        out.append(
            Quote(
                symbol=row[0],
                name=row[1] or row[0],
                sector="",
                listed="KSE100",
                current=current,
                change=change or 0.0,
                pct=percent,
                volume=volume or 0.0,
                weight=weight,
                mcap=num(row[10]) if len(row) > 10 else None,
            )
        )
    return out


def fetch_sectors() -> list[SectorRow]:
    tables = parse_tables(psx_get("/sector-summary/sectorwise"))
    rows = tables[0] if tables else []
    out: list[SectorRow] = []
    for row in rows:
        if len(row) < 7:
            continue
        adv, dec, unch, turnover, mcap = num(row[2]), num(row[3]), num(row[4]), num(row[5]), num(row[6])
        out.append(
            SectorRow(
                code=row[0],
                name=row[1].title(),
                advance=int(adv or 0),
                decline=int(dec or 0),
                unchanged=int(unch or 0),
                turnover=turnover or 0.0,
                mcap=mcap or 0.0,
            )
        )
    return out


def fetch_histories(members: list[Quote], market_day: date) -> list[StockHistory]:
    members = [q for q in members if not q.symbol.endswith("XD")]
    by_symbol = {q.symbol: q for q in members}
    histories: list[StockHistory] = []

    def one(symbol: str) -> StockHistory | None:
        series = fetch_psx_eod(symbol)
        last_day, last_close = series.closes[-1]
        if (market_day - last_day).days > 10:
            return None
        closes = [c for _, c in series.closes]
        ma15 = moving_average(closes, 15)
        ma50 = moving_average(closes, 50)
        ma200 = moving_average(closes, 200)
        quote = by_symbol[symbol]
        return StockHistory(
            symbol=symbol,
            name=quote.name,
            sector=quote.sector,
            last=last_close,
            day_pct=quote.pct,
            r15d=ret(closes, 15),
            r1m=ret(closes, 21),
            r3m=ret(closes, 63),
            r6m=ret(closes, 126),
            r1y=ret(closes, 252),
            vs_15=None if ma15 is None else pct(last_close - ma15, ma15),
            vs_50=None if ma50 is None else pct(last_close - ma50, ma50),
            vs_200=None if ma200 is None else pct(last_close - ma200, ma200),
            above_200=bool(ma200 is not None and last_close > ma200),
            volume=quote.volume,
            mcap=quote.mcap,
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(one, q.symbol): q.symbol for q in members}
        for fut in as_completed(futs):
            symbol = futs[fut]
            try:
                item = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"History skipped ({symbol}): {exc}", file=sys.stderr)
                continue
            if item:
                histories.append(item)
    return histories


def classify_regime(price: float, ma50: float | None, ma200: float | None) -> tuple[str, str, str]:
    if ma200 is None:
        return (
            "NOT ENOUGH HISTORY YET",
            "Just watch. We do not have a long enough chart to judge the trend.",
            "The 200-day average is a slow moving line of the last ~200 trading days. We need that line before calling a trend.",
        )
    above_200 = price > ma200
    if ma50 is not None and ma50 > ma200 and above_200:
        return (
            "UP TREND",
            "The big picture is rising. That still does not mean 'buy today'.",
            "KSE-100 is above its 200-day average, and the 50-day average is also above the 200-day. In simple words: the market has been generally going up.",
        )
    if ma50 is not None and ma50 < ma200 and not above_200:
        return (
            "DOWN TREND",
            "The big picture is falling. This is a learning period, not a shopping day.",
            "KSE-100 is below its 200-day average, and the 50-day average is also below it. In simple words: the market has been generally going down.",
        )
    if above_200:
        return (
            "MIXED (still above the long average)",
            "Do not chase a bounce. Watch how it behaves around the 200-day line.",
            "Price is still above the slow 200-day line, but the shorter trend is messy. This zone whipsaws beginners.",
        )
    return (
        "MIXED (below the long average)",
        "Sit in cash and learn. Waiting is a valid choice.",
        "Price is under the 200-day average. Until it climbs back and stays there, 'the market is going up' is not proven.",
    )


def headline_tone(title: str) -> str:
    lowered = title.lower()
    pos = sum(1 for w in POS_WORDS if w in lowered)
    neg = sum(1 for w in NEG_WORDS if w in lowered)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


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
        if not title:
            continue
        headlines.append(
            Headline(
                source=source,
                title=title,
                link=local_text(item.find("link")),
                published=local_text(item.find("pubDate")),
                tone=headline_tone(title),
            )
        )
    return headlines


def fetch_headlines(limit: int = 12) -> list[Headline]:
    buckets: list[list[Headline]] = []
    for source, url in NEWS_FEEDS:
        try:
            items = parse_rss(source, http_get(url))
        except Exception as exc:  # noqa: BLE001
            print(f"News feed skipped ({source}): {exc}", file=sys.stderr)
            continue
        related = [item for item in items if looks_market_related(item.title)]
        buckets.append(related or items[:5])
    collected: list[Headline] = []
    seen: set[str] = set()
    index = 0
    while len(collected) < limit and buckets:
        progressed = False
        for bucket in buckets:
            if index >= len(bucket):
                continue
            item = bucket[index]
            progressed = True
            key = item.title.casefold()
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


def score_sentiment(
    day_pct: float,
    price: float,
    ma50: float | None,
    ma200: float | None,
    quotes: list[Quote],
    headlines: list[Headline],
) -> tuple[int, str, list[str]]:
    score = 50
    notes: list[str] = []
    if day_pct > 0.4:
        score += 12
        notes.append(f"The index is up {day_pct:+.2f}% today. People feel a bit braver.")
    elif day_pct < -0.4:
        score -= 12
        notes.append(f"The index is down {day_pct:+.2f}% today. People feel more scared.")
    else:
        notes.append(f"The index is almost flat ({day_pct:+.2f}%). One quiet day does not make a trend.")

    if ma200 is not None:
        if price > ma200:
            score += 10
            notes.append(f"KSE-100 is {pct(price - ma200, ma200):+.1f}% above the 200-day average (the long trend is still up).")
        else:
            score -= 10
            notes.append(f"KSE-100 is {pct(price - ma200, ma200):+.1f}% below the 200-day average (the long trend is hurt).")
    if ma50 is not None and ma200 is not None:
        if ma50 > ma200:
            score += 8
            notes.append("The medium-term line (50-day) is still above the long-term line (200-day).")
        else:
            score -= 8
            notes.append("The medium-term line (50-day) has fallen below the long-term line (200-day).")

    if quotes:
        up = sum(1 for q in quotes if q.pct > 0)
        down = sum(1 for q in quotes if q.pct < 0)
        total = up + down
        if total:
            breadth = (up - down) / total
            score += int(breadth * 14)
            notes.append(f"Across the market, {up} stocks went up and {down} went down. That is called breadth.")

    pos_n = sum(1 for h in headlines if h.tone == "positive")
    neg_n = sum(1 for h in headlines if h.tone == "negative")
    score += (pos_n - neg_n) * 2
    notes.append(f"In today's headlines: {pos_n} sounded hopeful, {neg_n} sounded worried, {len(headlines) - pos_n - neg_n} were mixed.")

    score = max(5, min(95, score))
    if score >= 72:
        label = "HOPEFUL"
    elif score >= 58:
        label = "CAUTIOUSLY HOPEFUL"
    elif score >= 45:
        label = "MIXED / CALM"
    elif score >= 32:
        label = "CAUTIOUS / WORRIED"
    else:
        label = "SCARED"
    return score, label, notes


def build_playbook(digest_regime: str, day_pct: float, score: int) -> tuple[list[str], list[str]]:
    playbook = [
        "**You have not started buying yet. That is fine.** Today's job is to read this note, not to place an order.",
        "**Cash in your new broker account can wait.** You do not get a prize for buying in the first week.",
        "**Learn three things:** (1) did the whole market go up or down? (2) is KSE-100 above or below the **15-day** line (recent 2–3 weeks)? (3) is it above or below the **200-day** line (slow long trend)?",
    ]
    if digest_regime == "DOWN TREND" or score < 32:
        playbook.append("**Mood is weak.** This is a good week to watch fear without spending money. Falling prices feel like a sale; for a beginner they are usually a test of patience.")
        invest = [
            "Do **not** buy a big amount in one click.",
            "If you study names, pick **big KSE-100 companies** from your watchlist, not the day's +10% penny stocks.",
            "Write one sentence: *why would I still want this company in 3 years?* If you cannot, skip it.",
        ]
    elif "below the long average" in digest_regime or score < 45:
        playbook.append("**The long trend is not proven up.** Waiting is a real decision. You are collecting weeks of notes so your first buy is not a guess.")
        invest = [
            "Stay in **learning mode**. A small 'practice' buy is optional later — not today by default.",
            "Use the **healthier long-term names** table as a study list, not a shopping cart.",
            "Ignore limit-up junk. Fast +10% names are usually a trap for new accounts.",
        ]
    elif digest_regime.startswith("MIXED"):
        playbook.append("**The market is in-between.** Beginners lose money trying to look clever on messy days. Watch, don't poke.")
        invest = [
            "No lump-sum. If you ever buy, it should be after you can explain the 200-day line in your own words.",
            "Keep studying the watchlist on both red and green days so you see both moods.",
        ]
    else:
        playbook.append("**The long trend looks up**, so the class is easier to enjoy — still do not rush a first purchase.")
        invest = [
            "Green markets make people feel late. That feeling is how beginners overpay.",
            "Keep using the watchlist. A first buy, when you are ready, should be a name you have watched for weeks, not today's hero.",
        ]
    if abs(day_pct) >= 1.5:
        playbook.append(
            f"**Big day ({day_pct:+.2f}%).** Moves this large are mostly emotion. Sleep on it. Do not open the buy ticket."
        )
    return playbook, invest


def strong_names(histories: list[StockHistory]) -> list[StockHistory]:
    """Bigger, more liquid KSE-100 names with a boring-up trend — not 1-year rockets."""
    eligible = []
    for h in histories:
        if not h.above_200:
            continue
        if (h.r6m or 0) <= 0 or (h.r1y or 0) <= 0:
            continue
        if (h.r1y or 0) > 80 or (h.r6m or 0) > 60:
            continue
        if h.volume < 100_000:
            continue
        if h.mcap is not None and h.mcap < 15_000:
            continue
        eligible.append(h)
    return sorted(eligible, key=lambda h: (h.r1y or -999, h.r6m or -999), reverse=True)


def attach_sectors(quotes: list[Quote], kse100: list[Quote], sectors: list[SectorRow]) -> None:
    names = {s.code: s.name for s in sectors}
    by_sym = {q.symbol: q for q in quotes}
    for member in kse100:
        spot = by_sym.get(member.symbol)
        if spot:
            member.sector = names.get(spot.sector, spot.sector)
            member.listed = spot.listed
            member.volume = spot.volume or member.volume
    for q in quotes:
        q.sector = names.get(q.sector, q.sector)


WATCHLIST_FILE = Path("watchlist.txt")
SNAPSHOT_FILE = REPORTS_DIR / "snapshot.json"
DEFAULT_WATCHLIST = ("OGDC", "PPL", "HUBC", "LUCK", "MEBL", "UBL", "ENGRO", "SYS")


def load_watchlist() -> list[str]:
    if not WATCHLIST_FILE.exists():
        return list(DEFAULT_WATCHLIST)
    symbols: list[str] = []
    for raw in WATCHLIST_FILE.read_text(encoding="utf-8").splitlines():
        token = raw.split("#", 1)[0].strip().upper()
        if token and token not in symbols:
            symbols.append(token)
    return symbols or list(DEFAULT_WATCHLIST)


def load_previous_snapshot() -> dict | None:
    if not SNAPSHOT_FILE.exists():
        return None
    try:
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_snapshot(d: Digest) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": d.index.last[0].isoformat(),
        "kse100": d.index.last[1],
        "day_pct": d.day_pct,
        "sentiment": d.sentiment_score,
        "regime": d.regime,
        "strong": [h.symbol for h in strong_names(d.histories)[:10]],
    }
    SNAPSHOT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def describe_changes(d: Digest, prev: dict | None) -> list[str]:
    if not prev:
        return ["This is the first saved snapshot. From tomorrow, this box will say what changed."]
    lines: list[str] = []
    old_px = prev.get("kse100")
    if isinstance(old_px, (int, float)) and old_px:
        diff = d.index.last[1] - float(old_px)
        lines.append(
            f"KSE-100 vs last report: {signed(diff)} ({fmt_pct(pct(diff, float(old_px)))})."
        )
    old_sent = prev.get("sentiment")
    if isinstance(old_sent, (int, float)):
        delta = d.sentiment_score - int(old_sent)
        if delta > 3:
            lines.append(f"Mood score rose {delta:+d} (was {int(old_sent)}, now {d.sentiment_score}). People got a bit braver.")
        elif delta < -3:
            lines.append(f"Mood score fell {delta:+d} (was {int(old_sent)}, now {d.sentiment_score}). People got more worried.")
        else:
            lines.append(f"Mood score is almost unchanged ({d.sentiment_score} vs {int(old_sent)}).")
    old_reg = str(prev.get("regime") or "")
    if old_reg and old_reg != d.regime:
        lines.append(f"Trend label changed: {old_reg} → {d.regime}.")
    old_strong = [str(s) for s in prev.get("strong") or []]
    now_strong = [h.symbol for h in strong_names(d.histories)[:10]]
    added = [s for s in now_strong if s not in old_strong]
    dropped = [s for s in old_strong if s not in now_strong]
    if added:
        lines.append("Newly on the 'healthier long-term' study list: " + ", ".join(f"`{s}`" for s in added) + ".")
    if dropped:
        lines.append("Fell off that study list: " + ", ".join(f"`{s}`" for s in dropped) + ".")
    if len(lines) == 1:
        lines.append("No big change in the study-list names.")
    return lines


def pick_lesson(d: Digest) -> str:
    if abs(d.day_pct) >= 1.5:
        return (
            f"A {fmt_pct(d.day_pct)} day feels huge. For a long-term investor it is still one candle. "
            "Notice how news headlines sound more extreme on days like this."
        )
    if d.ma200 is not None and d.index.last[1] < d.ma200:
        return (
            "The 200-day average is a slow line. When price is under it, the market has to *earn* "
            "the right to be called an uptrend again. You can wait while it tries."
        )
    if d.quotes:
        up = sum(1 for q in d.quotes if q.pct > 0)
        down = sum(1 for q in d.quotes if q.pct < 0)
        if down > up * 3:
            return (
                "Breadth: the index is one number, but most stocks went down. "
                "That means the 'market' was weak, not just one heavyweight."
            )
    return (
        "Practice naming what you see — up/down, above/below the 200-day line, scary vs hopeful news — "
        "before you ever press buy."
    )


def expand_members_with_watchlist(kse100: list[Quote], quotes: list[Quote], symbols: list[str]) -> list[Quote]:
    have = {q.symbol.upper() for q in kse100}
    by_mw = {q.symbol.upper(): q for q in quotes}
    extra: list[Quote] = []
    for symbol in symbols:
        if symbol in have:
            continue
        spot = by_mw.get(symbol)
        extra.append(
            spot
            if spot
            else Quote(symbol=symbol, name=symbol, sector="", listed="WATCH", current=0.0, change=0.0, pct=0.0, volume=0.0)
        )
    return kse100 + extra


def index_change(series: QuoteSeries) -> tuple[str, float, float, float | None, float | None, float | None, float | None]:
    closes = [c for _, c in series.closes]
    last_day, last_close = series.last
    eod_day, eod_close = series.closes[-1]
    if len(closes) < 2:
        prev = last_close
    else:
        prev = eod_close if last_day != eod_day else closes[-2]
    change = last_close - prev
    day_pct = pct(change, prev)
    label = "UP" if change > 0 else "DOWN" if change < 0 else "FLAT"
    return (
        label,
        change,
        day_pct,
        ret(closes, 5),
        moving_average(closes, 20),
        moving_average(closes, 50),
        moving_average(closes, 200),
    )


def collect() -> Digest:
    warmup_psx()
    index = load_index("KSE100")
    kse30 = load_optional_index("KSE30")
    allshr = load_optional_index("ALLSHR")
    quotes: list[Quote] = []
    kse100: list[Quote] = []
    sectors: list[SectorRow] = []
    try:
        quotes = fetch_market_watch()
    except Exception as exc:  # noqa: BLE001
        print(f"Market watch skipped: {exc}", file=sys.stderr)
    try:
        kse100 = fetch_kse100_board()
    except Exception as exc:  # noqa: BLE001
        print(f"KSE-100 board skipped: {exc}", file=sys.stderr)
    try:
        sectors = fetch_sectors()
    except Exception as exc:  # noqa: BLE001
        print(f"Sector summary skipped: {exc}", file=sys.stderr)
    attach_sectors(quotes, kse100, sectors)
    watch_symbols = load_watchlist()
    members = expand_members_with_watchlist(kse100, quotes, watch_symbols)
    headlines = fetch_headlines(12)
    label, day_change, day_pct, five_pct, ma20, ma50, ma200 = index_change(index)
    fifteen_pct, ma15, _vs15 = series_window(index, 15)
    last_price = index.last[1]
    regime, stance, why = classify_regime(last_price, ma50, ma200)
    score, sent_label, sent_notes = score_sentiment(day_pct, last_price, ma50, ma200, quotes, headlines)
    playbook, invest_today = build_playbook(regime, day_pct, score)
    market_day = index.closes[-1][0]
    stale_days = (datetime.now(PKT).date() - market_day).days
    closed_note = ""
    if stale_days >= 4:
        closed_note = (
            f"Last index session in the data is {market_day.isoformat()} ({stale_days} days ago). "
            "The market may have been closed (weekend or holiday). Treat numbers as stale."
        )
    print(f"Fetching 1-year history for {len(members)} names...", file=sys.stderr)
    histories = fetch_histories(members, market_day)
    by_sym = {h.symbol.upper(): h for h in histories}
    watchlist = [by_sym[s] for s in watch_symbols if s in by_sym]
    previous = load_previous_snapshot()
    digest = Digest(
        generated=datetime.now(PKT).strftime("%Y-%m-%d %H:%M PKT"),
        index=index,
        kse30=kse30,
        allshr=allshr,
        quotes=quotes,
        kse100=kse100,
        sectors=sectors,
        histories=histories,
        headlines=headlines,
        day_label=label,
        day_change=day_change,
        day_pct=day_pct,
        five_pct=five_pct,
        fifteen_pct=fifteen_pct,
        ma15=ma15,
        ma20=ma20,
        ma50=ma50,
        ma200=ma200,
        regime=regime,
        stance=stance,
        why=why,
        sentiment_score=score,
        sentiment_label=sent_label,
        sentiment_detail=sent_notes,
        playbook=playbook,
        invest_today=invest_today,
        watchlist=watchlist,
        vs_yesterday=[],
        lesson="",
        closed_note=closed_note,
    )
    digest.vs_yesterday = describe_changes(digest, previous)
    digest.lesson = pick_lesson(digest)
    r15, _ma15, vs15 = series_window(index, 15)
    if r15 is not None:
        digest.sentiment_detail.append(
            f"Over the last ~15 sessions the index is {fmt_pct(r15)} (short window, about 2–3 weeks)."
        )
    if vs15 is not None:
        if vs15 >= 0:
            digest.sentiment_detail.append(
                f"KSE-100 is {fmt_pct(vs15)} above the 15-day average (recent trend not broken)."
            )
        else:
            digest.sentiment_detail.append(
                f"KSE-100 is {abs(vs15):.2f}% below the 15-day average (the last 2–3 weeks are weak)."
            )
    return digest


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = "\n".join("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join([head, sep, body]) if rows else "_No rows._"


def index_line(series: QuoteSeries | None, fallback: str) -> str:
    if series is None:
        return fallback
    label, change, day_pct, five, *_rest, ma200 = index_change(series)
    r15, _ma15, vs15 = series_window(series, 15)
    vs = "" if ma200 is None else f" | vs 200d {fmt_pct(pct(series.last[1] - ma200, ma200))}"
    vs15_s = "" if vs15 is None else f" | vs 15d {fmt_pct(vs15)}"
    five_s = "" if five is None else f" | 5d {fmt_pct(five)}"
    r15_s = "" if r15 is None else f" | 15d {fmt_pct(r15)}"
    return f"{series.symbol} **{fmt(series.last[1])}** · {label} {signed(change)} ({fmt_pct(day_pct)}){five_s}{r15_s}{vs15_s}{vs}"


def top(items: list, key, reverse: bool = True, n: int = 8):
    return sorted(items, key=key, reverse=reverse)[:n]


def build_report(d: Digest) -> str:
    last_day, last_close = d.index.last
    vs_200 = "n/a" if d.ma200 is None else fmt_pct(pct(last_close - d.ma200, d.ma200))
    vs_15 = "n/a" if d.ma15 is None else fmt_pct(pct(last_close - d.ma15, d.ma15))
    r15 = "n/a" if d.fifteen_pct is None else fmt_pct(d.fifteen_pct)
    vol20 = fmt(statistics.pstdev([c for _, c in d.index.closes][-20:]), 1) if len(d.index.closes) >= 20 else "n/a"

    kse_quotes = [q for q in (d.kse100 or [q for q in d.quotes if "KSE100" in q.listed]) if not q.symbol.endswith("XD")]
    gainers = top(kse_quotes, key=lambda q: q.pct, n=8)
    losers = top(kse_quotes, key=lambda q: q.pct, reverse=False, n=8)
    active = top(kse_quotes, key=lambda q: q.volume, n=8)
    strong = strong_names(d.histories)[:10]
    extended = [h for h in strong if (h.vs_50 or 0) > 20]
    buyable = [h for h in strong if (h.vs_50 or 0) <= 12][:8]
    weak_hist = sorted(
        [h for h in d.histories if not h.above_200],
        key=lambda h: (h.r1y or 0),
    )[:6]

    hot_sectors = [s for s in top(d.sectors, key=lambda s: s.advance - s.decline, n=8) if s.advance > s.decline][:6]
    cold_sectors = top(d.sectors, key=lambda s: s.advance - s.decline, reverse=False, n=6)

    junk = [
        q for q in top(d.quotes, key=lambda q: q.pct, n=15)
        if "KSE100" not in q.listed and q.pct >= 7
    ][:6]

    def qrows(items: list[Quote]) -> list[list[str]]:
        return [[q.symbol, q.name[:28], q.sector[:22], fmt(q.current), fmt_pct(q.pct), f"{q.volume:,.0f}"] for q in items]

    def hrows(items: list[StockHistory]) -> list[list[str]]:
        return [[
            h.symbol,
            h.name[:26],
            fmt(h.last),
            fmt_pct(h.day_pct),
            fmt_pct(h.r15d),
            fmt_pct(h.r1m),
            fmt_pct(h.r3m),
            fmt_pct(h.r6m),
            fmt_pct(h.r1y),
            fmt_pct(h.vs_15),
            fmt_pct(h.vs_200),
        ] for h in items]

    news_lines = []
    for item in d.headlines:
        tag = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}[item.tone]
        extra = f" _{item.published}_" if item.published else ""
        title = f"[{item.title}]({item.link})" if item.link else item.title
        news_lines.append(f"- {tag} **{item.source}:** {title}{extra}")

    playbook = "\n".join(f"{i}. {line}" for i, line in enumerate(d.playbook, 1))
    invest = "\n".join(f"- {line}" for line in d.invest_today)
    sentiment = "\n".join(f"- {line}" for line in d.sentiment_detail)
    changed = "\n".join(f"- {line}" for line in d.vs_yesterday)
    closed = f"\n\n> {d.closed_note}\n" if d.closed_note else ""
    avoid = "\n".join(
        f"- `{q.symbol}` ({q.sector or 'n/a'}) {fmt_pct(q.pct)} — not in KSE-100. For a beginner this is gambling, not investing."
        for q in junk
    ) or "- No extreme non-index rocket names stood out."
    weak_s = "\n".join(
        f"- `{h.symbol}` {h.name[:40]} · 1y {fmt_pct(h.r1y)} · still below the 200-day line"
        for h in weak_hist
    ) or "- None flagged."
    watch_rows = hrows(d.watchlist) if d.watchlist else []

    kse30 = index_line(d.kse30, "KSE-30 n/a")
    allshr = index_line(d.allshr, "ALLSHR n/a")

    return f"""# Daily PSX class notes — {last_day.isoformat()}

**Generated:** {d.generated}  
**For:** someone who just opened an account and is **learning for a few weeks before buying**.  
**Not financial advice.** You can lose money. One day is not a plan.
{closed}
---

## If you have not bought anything yet

{playbook}

**Today's lesson:** {d.lesson}

---

## In one minute

| Question | Today |
| --- | --- |
| Did the whole market go up or down? | **{d.day_label}** {fmt_pct(d.day_pct)} |
| KSE-100 level | **{fmt(last_close)}** |
| Last ~15 sessions | {r15} |
| Above or below the 15-day line? | {vs_15} |
| Above or below the 200-day line? | {vs_200} |
| Mood (0 scared → 100 hopeful) | **{d.sentiment_label}** ({d.sentiment_score}/100) |
| Simple stance | {d.stance} |

KSE-100 is the scoreboard of Pakistan's 100 bigger companies. If it falls, most portfolios feel it.

- {kse30}
- {allshr}

{d.why}

---

## What changed since last report

{changed}

---

## Why the mood looks like this

{sentiment}

If the index, the 15-day line, the 200-day line, most stocks, *and* the news all agree, trust the mood more. If they argue, just watch.

---

## Andaza (kal, ~15 din, 200 din)

Working hypothesis, **not a forecast you should trade**. Tomorrow can bounce or continue; the 15-day line is the near mood (~2–3 weeks); the 200-day line is the long picture.

{ru_outlook(d, "learner")}

---

## Your watchlist

These are **study names** from `watchlist.txt` (big, commonly discussed companies). Edit that file anytime. This is not a buy list.

{md_table(["Symbol", "Name", "Last", "Day", "15d", "1m", "3m", "6m", "1y", "vs 15d", "vs 200d"], watch_rows) if watch_rows else "_Could not load watchlist prices today._"}

How to read a row: **Day** = today. **15d** = last ~15 sessions. **1m / 6m / 1y** = last month / ~6 months / ~1 year. **vs 15d / vs 200d** = above (+) or below (−) those average lines.

---

## Healthier long-term KSE-100 names (study list)

Filter (not magic): still above the 200-day line, up over ~6 months and ~1 year, **not** a crazy rocket (+80% in a year is excluded), and big/liquid enough for a beginner to learn on.

{invest}

{md_table(["Symbol", "Name", "Last", "Day", "15d", "1m", "3m", "6m", "1y", "vs 15d", "vs 200d"], hrows(strong))}

Quieter of those (not running too far ahead of the 50-day line):

{md_table(["Symbol", "Name", "Last", "Day", "15d", "1m", "3m", "6m", "1y", "vs 15d", "vs 200d"], hrows(buyable))}

{"These look stretched (wait, don't chase): " + ", ".join(f"`{h.symbol}`" for h in extended) if extended else "None of the study names look wildly stretched vs the 50-day line."}

Weaker KSE-100 names still **under** the 200-day line (study why, don't 'catch the falling knife'):

{weak_s}

Hot names **outside** KSE-100 (easy trap):

{avoid}

---

## Today's KSE-100 tape

Green/red today is noise. Use it to *notice* sectors, not to buy.

### Up today
{md_table(["Symbol", "Name", "Sector", "Last", "Day", "Volume"], qrows(gainers))}

### Down today
{md_table(["Symbol", "Name", "Sector", "Last", "Day", "Volume"], qrows(losers))}

### Most traded
{md_table(["Symbol", "Name", "Sector", "Last", "Day", "Volume"], qrows(active))}

---

## Sectors (groups of similar companies)

If more stocks in a sector went up than down, that group had a better day.

### Relatively firm
{md_table(["Sector", "Up", "Down", "Flat", "Turnover", "Mcap (B)"],
    [[s.name, str(s.advance), str(s.decline), str(s.unchanged), f"{s.turnover:,.0f}", fmt(s.mcap)] for s in hot_sectors]) if hot_sectors else "_No sector had more stocks up than down today._"}

### Soft
{md_table(["Sector", "Up", "Down", "Flat", "Turnover", "Mcap (B)"],
    [[s.name, str(s.advance), str(s.decline), str(s.unchanged), f"{s.turnover:,.0f}", fmt(s.mcap)] for s in cold_sectors])}

---

## News (simple tags)

🟢 sounded hopeful · 🔴 sounded worried · ⚪ mixed. This is a keyword skim of the headline, not a full article.

{chr(10).join(news_lines) if news_lines else "- No headlines could be fetched today."}

---

## Words to know

- **KSE-100** — Pakistan's main stock index. One number for 100 larger companies.
- **15-day average** — near trend line (~2–3 weeks). Turns faster than the 200-day.
- **200-day average** — slow trend line. Above it ≈ long uptrend; below it ≈ long downtrend.
- **50-day average** — faster trend line.
- **Breadth** — how many stocks went up vs down, not just the index.
- **Volume** — how many shares traded. Very low volume + huge % move is often a trap.
- **Watchlist** — names you track without buying yet.

## How to use these notes for a few weeks

1. Read **If you have not bought anything yet** first. Do not open a buy order because a table looks green.
2. Each day, say out loud: up or down? above or below 15-day? above or below 200-day? scared or hopeful news?
3. Follow your watchlist like homework. After 10–15 notes you will see the same names in different moods.
4. When you *do* start, start tiny, in a name you already understand, with money you can leave for years.
5. Confirm the last price with your broker. This file is delayed public data.
"""


def build_telegram_parts(d: Digest) -> list[str]:
    last_close = d.index.last[1]
    vs_200 = "n/a" if d.ma200 is None else fmt_pct(pct(last_close - d.ma200, d.ma200))
    url = os.environ.get(
        "DIGEST_REPORT_URL",
        "https://github.com/AbuzarGhazanfarKhan/psx-daily-digest/blob/main/reports/latest.md",
    )
    strong = strong_names(d.histories)[:6]
    kse_quotes = [q for q in (d.kse100 or [q for q in d.quotes if "KSE100" in q.listed]) if not q.symbol.endswith("XD")]
    gainers = top(kse_quotes, key=lambda q: q.pct, n=4)
    losers = top(kse_quotes, key=lambda q: q.pct, reverse=False, n=4)
    closed = f"\nNote: {d.closed_note}\n" if d.closed_note else ""

    def hist_lines(items: list[StockHistory]) -> str:
        if not items:
            return "(none today)"
        return "\n".join(
            f"{h.symbol}  day {fmt_pct(h.day_pct)}  1y {fmt_pct(h.r1y)}  vs200d {fmt_pct(h.vs_200)}"
            for h in items
        )

    part1 = (
        f"PSX CLASS NOTES  {d.generated}\n"
        f"You have not started buying. Read. Do not order.\n"
        f"Not financial advice. You can lose money.\n"
        f"{closed}\n"
        f"KSE-100  {fmt(last_close)}\n"
        f"Today    {d.day_label}  {fmt_pct(d.day_pct)}\n"
        f"vs 200-day line  {vs_200}\n"
        f"Mood     {d.sentiment_label}  ({d.sentiment_score}/100)\n"
        f"Trend    {d.regime}\n\n"
        f"TODAY'S LESSON\n{d.lesson}\n\n"
        f"WHAT TO DO (learning weeks)\n"
        + "\n".join(f"{i}. {strip_md(p)}" for i, p in enumerate(d.playbook, 1))
    )
    part2 = "WHAT CHANGED\n" + "\n".join(f"• {strip_md(x)}" for x in d.vs_yesterday)
    part2 += "\n\nYOUR WATCHLIST (study, don't buy)\n" + hist_lines(d.watchlist)
    part2 += "\n\nHEALTHIER LONG-TERM NAMES (study list)\n" + hist_lines(strong)
    part3 = "KSE-100 TODAY\nUp:\n" + "\n".join(f"▲ {q.symbol} {fmt_pct(q.pct)}" for q in gainers)
    part3 += "\nDown:\n" + "\n".join(f"▼ {q.symbol} {fmt_pct(q.pct)}" for q in losers)
    part3 += "\n\nNEWS\n"
    for item in d.headlines[:5]:
        mark = {"positive": "+", "negative": "-", "neutral": "·"}[item.tone]
        part3 += f"{mark} {item.title}\n"
    part3 += f"\nFull notes with tables:\n{url}"
    return [part1, part2, part3]


def strip_md(text: str) -> str:
    return text.replace("**", "").replace("`", "")


def _tape(d: Digest) -> dict:
    last_close = d.index.last[1]
    vs_200 = None if d.ma200 is None else pct(last_close - d.ma200, d.ma200)
    up = sum(1 for q in d.quotes if q.pct > 0)
    down = sum(1 for q in d.quotes if q.pct < 0)
    kse_quotes = [q for q in (d.kse100 or [q for q in d.quotes if "KSE100" in q.listed]) if not q.symbol.endswith("XD")]
    strong = strong_names(d.histories)[:8]
    return {
        "last": last_close,
        "vs_200": vs_200,
        "up": up,
        "down": down,
        "gainers": top(kse_quotes, key=lambda q: q.pct, n=6),
        "losers": top(kse_quotes, key=lambda q: q.pct, reverse=False, n=6),
        "strong": strong,
        "active": top(kse_quotes, key=lambda q: q.volume, n=6),
        "hot_sectors": [s for s in top(d.sectors, key=lambda s: s.advance - s.decline, n=6) if s.advance > s.decline][:5],
        "cold_sectors": top(d.sectors, key=lambda s: s.advance - s.decline, reverse=False, n=5),
    }


def ru_vs_200(vs: float | None) -> str:
    if vs is None:
        return "200-day average ka data available nahi."
    if vs >= 0:
        return (
            f"KSE-100 is waqt 200-day average se {fmt_pct(vs)} UPAR hai — "
            "lambi muddat ka chart generally up ki taraf ishara deta hai."
        )
    return (
        f"KSE-100 is waqt 200-day average se {abs(vs):.2f}% NEECHE hai — "
        "long uptrend prove nahi hua."
    )


def ru_vs_15(d: Digest) -> str:
    vs = None if d.ma15 is None else pct(d.index.last[1] - d.ma15, d.ma15)
    r15 = d.fifteen_pct
    extra = "" if r15 is None else f" Pichle ~15 sessions ka return {fmt_pct(r15)} hai."
    if vs is None:
        return "15-day average ka data available nahi."
    if vs >= 0:
        return (
            f"15-day average se {fmt_pct(vs)} UPAR hai — qareeb ki 2–3 weeks ka mood itna bura nahi.{extra}"
        )
    return (
        f"15-day average se {abs(vs):.2f}% NEECHE hai — qareeb ki 2–3 weeks kamzor hain.{extra}"
    )


def ru_news_theme(d: Digest) -> str:
    pos_n = sum(1 for h in d.headlines if h.tone == "positive")
    neg_n = sum(1 for h in d.headlines if h.tone == "negative")
    mix_n = len(d.headlines) - pos_n - neg_n
    blob = " ".join(h.title.lower() for h in d.headlines)
    geo = any(
        w in blob
        for w in ("war", "conflict", "fear", "uncertain", "middle east", "mideast", "geopol")
    )
    line = (
        f"Headlines ka keyword skim: {pos_n} hopeful, {neg_n} worried, {mix_n} mixed. "
        "Yeh poora article nahi, sirf title ka andaaz."
    )
    if geo:
        line += (
            " Titles mein war / conflict / uncertainty jaisa mood hai. "
            "Aise themes aksar kai sessions tak market ko restless rakhte hain, ek raat mein khatam nahi hote."
        )
    elif neg_n > pos_n:
        line += " Worried titles zyada hain, is liye kal ka open bhi defensive ho sakta hai."
    elif pos_n > neg_n:
        line += " Hopeful titles zyada hain, lekin yeh buy signal nahi."
    return line


def ru_outlook(d: Digest, audience: str) -> str:
    """Working andaza — not a tradeable forecast."""
    t = _tape(d)
    vs200 = t["vs_200"]
    vs15 = None if d.ma15 is None else pct(d.index.last[1] - d.ma15, d.ma15)
    below_200 = vs200 is not None and vs200 < 0
    below_15 = vs15 is not None and vs15 < 0
    wide = abs(d.day_pct) >= 1.5
    weak_breadth = t["down"] > max(t["up"], 1) * 3
    r15_weak = d.fifteen_pct is not None and d.fifteen_pct <= -2
    news_neg = sum(1 for h in d.headlines if h.tone == "negative") > sum(
        1 for h in d.headlines if h.tone == "positive"
    )

    if below_200 and (below_15 or d.sentiment_score < 32):
        stability = "unstable / restless"
        horizon_15 = (
            "Aane wale ~15 trading days mein base case yeh hai ke mood choppy / defensive rahe, "
            "jab tak price 15-day average ke qareeb wapas na aaye aur wahan tik na sake."
        )
        horizon_200 = (
            "200-day average ke neeche rehna weeks ka masla ho sakta hai. "
            "Long uptrend tab tak claim nahi karte jab yeh line cross ho aur upar hold ho."
        )
    elif below_200 and not below_15:
        stability = "short bounce, long still unproven"
        horizon_15 = (
            "Qareeb ki 15-day line ke upar bounce dikh raha hai, lekin yeh bounce fail bhi ho sakta hai "
            "kyunke 200-day abhi neeche hai."
        )
        horizon_200 = "Lambi line (200-day) recapture ke baghair 'market wapas uptrend mein hai' nahi kaha ja sakta."
    elif below_15 and not below_200:
        stability = "short-term dip inside a still-long chart"
        horizon_15 = (
            "200-day upar hai to lambi tasveer itni buri nahi, lekin 15-day ke neeche "
            "aane wale kuch sessions ordinary nahi, thora restless ho sakte hain."
        )
        horizon_200 = "Long trend abhi tootne ka faisla 15-day dip se nahi hota — pehle 200-day tootna padta hai."
    elif d.sentiment_score >= 58 and not wide:
        stability = "relatively ordinary / stable"
        horizon_15 = "Agar breadth aur headlines mil kar hopeful rahein to agle ~15 din ordinary sessions jaisa behave kar sakte hain."
        horizon_200 = "200-day ke upar rehna long chart ke haq mein hai; phir bhi ek tezz din usko palat nahi deta."
    else:
        stability = "mixed / watchful"
        horizon_15 = "15-day window mein dono taraf ke din mumkin hain — size chhota, assumptions kam."
        horizon_200 = ru_vs_200(vs200)

    if wide and d.day_pct < 0:
        kal = (
            f"Kal vs aaj: aaj {fmt_pct(d.day_pct)} ka chaurha din tha. "
            "Kal automatically aaj jaisa girna zaroori nahi, aur automatic bounce bhi zaroori nahi. "
            "Bari girawat ke baad kabhi short-covering (short positions band karna) se thora ubhaar dikhta hai, "
            "kabhi news ke sath pressure jari rehta hai. Dono mumkin hain."
        )
    elif wide and d.day_pct > 0:
        kal = (
            f"Kal vs aaj: aaj {fmt_pct(d.day_pct)} ka tez up din tha. "
            "Aisa din kal khud-ba-khud continue nahi hota, khas kar agar breadth kamzor ho."
        )
    elif r15_weak:
        kal = (
            "Kal vs aaj: pichle ~15 sessions pehle se kamzor hain, is liye kal ka default andaaz "
            "defensive hi rehne ka zyada ihtimal hai jab tak 15-day average wapas na milay."
        )
    else:
        kal = (
            "Kal vs aaj: pehla ishara wohi teen sawal honge — index up ya down, "
            "15-day ke kis taraf, headlines ka mood."
        )
    if weak_breadth:
        kal += f" Aaj breadth kamzor thi ({t['up']} up, {t['down']} down), is liye kisi bhi bounce ko naya accumulation na samjhein."
    if news_neg:
        kal += " News ka mood bhi defensive hai."

    who = (
        "Aap learning period mein hain. Is andaze par koi order na dein."
        if audience == "learner"
        else "Yeh working andaza hai, position ka hukm nahi. Size aur thesis aap ke hain."
    )
    return (
        f"Market ka haal (andaza, forecast nahi): {stability}.\n\n"
        f"{kal}\n\n"
        f"~15 din: {horizon_15}\n\n"
        f"200 din: {horizon_200}\n\n"
        f"{ru_news_theme(d)}\n\n"
        f"{who} Guarantee nahi. Financial advice nahi."
    )


def ru_lesson(d: Digest) -> str:
    if abs(d.day_pct) >= 1.5:
        return (
            f"Aaj ka move {fmt_pct(d.day_pct)} ehsas mein bara lagta hai. "
            "Long-term ke liye yeh abhi ek trading day hai. Headlines tez ho jati hain; unhe mood samajhein, buy signal nahi."
        )
    if d.ma200 is not None and d.index.last[1] < d.ma200:
        return (
            "Jab price 200-day average ke neeche ho, market ko wapas uptrend ke liye "
            "yeh line cross karni padti hai aur uske upar tikna padta hai."
        )
    if d.ma15 is not None and d.index.last[1] < d.ma15:
        return (
            "15-day average ke neeche short window kamzor hai. "
            "Pehle yeh qareeb ki line dekhein, phir 200-day."
        )
    return (
        "Roz teen sawal: market up hai ya down? 15-day average ke upar hai ya neeche? "
        "200-day average ke upar hai ya neeche?"
    )


def investor_ru_stance(d: Digest) -> list[str]:
    if d.regime.startswith("DOWN") or d.sentiment_score < 32:
        return [
            "Bias defensive rakhein. Decline ko automatic discount na samjhein.",
            "Lump-sum (ek hi dafa bari raqam) aaj munasiib nahi.",
            "Limit-up (~10% daily ceiling) aur low-volume spikes speculation hain.",
        ]
    if "below" in d.regime.lower() or d.sentiment_score < 45:
        return [
            "KSE-100 200-day average ke neeche hai, is liye long bull case confirm nahi.",
            "Default wait. Staged add sochiye to sirf liquid KSE-100 names, circuit-hitter nahi.",
            "Jo name 15-day ya 50-day se ziyada stretch ho, usko chase na karein.",
        ]
    if d.regime.startswith("UP"):
        return [
            "Long trend supportive hai; green session ka matlab har name buy nahi.",
            "Extended names par FOMO avoid karein.",
            "Agar ek stock portfolio ka disproportionate hissa ban jaye to rebalance par ghaur karein.",
        ]
    return [
        "Tape mixed hai. Bina plan ke hero trade se parhez karein.",
        "15-day aur 200-day ke qareeb whipsaw aam hai — size conservative rakhein.",
        "Jab tak 15-day aur 200-day dono ke upar na hon, dry powder rakhein.",
    ]


def split_telegram(text: str, limit: int = 3900) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            parts.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < 400:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    return parts


def learner_ru_parts(d: Digest) -> list[str]:
    t = _tape(d)
    closed = f"Note: {d.closed_note}\n\n" if d.closed_note else ""
    five = "" if d.five_pct is None else f" Pichli 5 sessions {fmt_pct(d.five_pct)}."
    p1 = (
        f"PSX class notes — {d.generated}\n"
        f"Yeh taleem ke liye hai, financial advice nahi. Shares mein loss mumkin hai.\n"
        f"{closed}"
        f"Aaj KSE-100 {d.day_label} {fmt_pct(d.day_pct)} raha, level {fmt(t['last'])}.{five}\n\n"
        f"KSE-100 kya hai? Pakistan Stock Exchange ki 100 bari companies ka scoreboard. "
        f"Agar yeh gire to aksar portfolios mein ehsas hota hai, chahe aap ne kuch khareeda na ho.\n\n"
        f"15-day average kya hai? Pichli ~15 trading days (qareeb 2–3 weeks) ki average price. "
        f"Yeh qareeb ka mood hai, jaldi gir/chad sakti hai. {ru_vs_15(d)}\n\n"
        f"200-day average kya hai? Pichli ~200 trading days ki ahista line. "
        f"Yeh lambi tasveer hai. {ru_vs_200(t['vs_200'])}\n\n"
        f"Breadth: aaj {t['up']} stocks up, {t['down']} down. "
        f"Agar zyada stocks down hon to pressure chhupa nahi, wus'at mein hai.\n\n"
        f"Sentiment {d.sentiment_score}/100 ({d.sentiment_label}) — andaza, guarantee nahi.\n\n"
        f"Aap ka account naya hai. Aaj buy order na dein. Cash reh sakti hai.\n"
        f"Aaj ki dars: {ru_lesson(d)}"
    )
    p2 = (
        f"{ru_outlook(d, 'learner')}\n\n"
        "Numbers aur tables is message ke sath attached HTML file mein hain — wahan watchlist, movers, sectors readable hain. "
        "Telegram wali chat mein list nahi bheji taake message saaf rahe."
    )
    return [p1, p2]


def investor_ru_parts(d: Digest) -> list[str]:
    t = _tape(d)
    closed = f"Note: {d.closed_note}\n\n" if d.closed_note else ""
    stance = "\n".join(f"{i}. {line}" for i, line in enumerate(investor_ru_stance(d), 1))
    five = "" if d.five_pct is None else f" Pichli 5 sessions {fmt_pct(d.five_pct)}."
    k30 = series_window(d.kse30, 15)
    alls = series_window(d.allshr, 15)
    k30s = ""
    if d.kse30 is not None and k30[0] is not None:
        k30s = f" KSE-30 pichle 15 sessions {fmt_pct(k30[0])}."
    alls_s = ""
    if d.allshr is not None and alls[0] is not None:
        alls_s = (
            f" ALLSHR (broad market) pichle 15 sessions {fmt_pct(alls[0])}. "
            "Agar ALLSHR KSE-100 se zyada gira ho to chhote names par pressure zyada."
        )
    p1 = (
        f"Investor note — {d.generated}\n"
        f"Research note hai, recommendation nahi. Capital at risk.\n"
        f"{closed}"
        f"Tape: KSE-100 {d.day_label} {fmt_pct(d.day_pct)}, level {fmt(t['last'])}.{five}{k30s}{alls_s}\n\n"
        f"15-day (qareeb): {ru_vs_15(d)}\n"
        f"200-day (lamba): {ru_vs_200(t['vs_200'])}\n"
        f"50-day vs 200-day: "
        f"{'50-day abhi 200-day ke upar (intermediate itna bura nahi).' if (d.ma50 and d.ma200 and d.ma50 > d.ma200) else '50-day 200-day ke qareeb/neeche — intermediate kamzor.'}\n\n"
        f"Breadth {t['up']} up / {t['down']} down. "
        f"Heavy decliners par bounce short-covering ho sakta hai, naya accumulation nahi.\n"
        f"Sentiment {d.sentiment_score}/100 ({d.sentiment_label}). Regime: {d.regime}.\n\n"
        f"Stance:\n{stance}"
    )
    p2 = (
        f"{ru_outlook(d, 'investor')}\n\n"
        "Quality screen, watchlist, volume aur sector tables attached HTML file mein hain."
    )
    return [p1, p2]


def learner_telegram_bundle(d: Digest) -> list[str]:
    parts: list[str] = []
    for block in learner_ru_parts(d):
        parts.extend(split_telegram(block))
    return parts


def investor_telegram_bundle(d: Digest) -> list[str]:
    parts: list[str] = []
    for block in investor_ru_parts(d):
        parts.extend(split_telegram(block))
    return parts


def ru_learner_markdown(d: Digest) -> str:
    return "\n\n".join(["## Roman Urdu", *learner_ru_parts(d)])


def ru_investor_markdown(d: Digest) -> str:
    stance = "\n".join(f"- {line}" for line in investor_ru_stance(d))
    return (
        f"# Investor note — {d.index.last[0].isoformat()}\n\n"
        f"## Stance\n{stance}\n\n"
        f"## Roman Urdu\n\n" + "\n\n".join(investor_ru_parts(d)) + "\n"
    )


def html_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "<p><em>No rows.</em></p>"
    th = "".join(f"<th>{html_lib.escape(h)}</th>" for h in headers)
    body = []
    for row in rows:
        tds = "".join(f"<td>{html_lib.escape(c)}</td>" for c in row)
        body.append(f"<tr>{tds}</tr>")
    return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def build_html_report(d: Digest, audience: str) -> str:
    last_day, last_close = d.index.last
    vs_200 = "n/a" if d.ma200 is None else fmt_pct(pct(last_close - d.ma200, d.ma200))
    vs_15 = "n/a" if d.ma15 is None else fmt_pct(pct(last_close - d.ma15, d.ma15))
    r15 = "n/a" if d.fifteen_pct is None else fmt_pct(d.fifteen_pct)
    kse_quotes = [q for q in (d.kse100 or [q for q in d.quotes if "KSE100" in q.listed]) if not q.symbol.endswith("XD")]
    hist_h = ["Symbol", "Name", "Last", "Day", "15d", "1m", "3m", "6m", "1y", "vs 15d", "vs 200d"]
    quote_h = ["Symbol", "Name", "Sector", "Last", "Day", "Volume"]

    def qrows(items: list[Quote]) -> list[list[str]]:
        return [[q.symbol, q.name[:28], q.sector[:22], fmt(q.current), fmt_pct(q.pct), f"{q.volume:,.0f}"] for q in items]

    def hrows(items: list[StockHistory]) -> list[list[str]]:
        return [[
            h.symbol, h.name[:26], fmt(h.last), fmt_pct(h.day_pct), fmt_pct(h.r15d),
            fmt_pct(h.r1m), fmt_pct(h.r3m), fmt_pct(h.r6m), fmt_pct(h.r1y),
            fmt_pct(h.vs_15), fmt_pct(h.vs_200),
        ] for h in items]

    strong = strong_names(d.histories)[:10]
    gainers = top(kse_quotes, key=lambda q: q.pct, n=8)
    losers = top(kse_quotes, key=lambda q: q.pct, reverse=False, n=8)
    active = top(kse_quotes, key=lambda q: q.volume, n=8)
    hot = [s for s in top(d.sectors, key=lambda s: s.advance - s.decline, n=8) if s.advance > s.decline][:6]
    cold = top(d.sectors, key=lambda s: s.advance - s.decline, reverse=False, n=6)
    title = "PSX learner tables" if audience == "learner" else "PSX investor tables"
    outlook = ru_outlook(d, audience).replace("\n", "<br>\n")
    news = "".join(
        f"<li>{html_lib.escape(item.source)}: {html_lib.escape(item.title)}</li>"
        for item in d.headlines[:10]
    )
    sector_h = ["Sector", "Up", "Down", "Flat"]
    hot_rows = [[s.name, str(s.advance), str(s.decline), str(s.unchanged)] for s in hot]
    cold_rows = [[s.name, str(s.advance), str(s.decline), str(s.unchanged)] for s in cold]
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(title)} — {last_day.isoformat()}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; margin: 16px; color: #111; line-height: 1.45; }}
h1 {{ font-size: 1.25rem; }}
h2 {{ font-size: 1.05rem; margin-top: 1.4rem; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin: 10px 0 18px; }}
th, td {{ border: 1px solid #ccc; padding: 6px 8px; text-align: left; }}
th {{ background: #f3f3f3; }}
.note {{ color: #444; font-size: 0.95rem; }}
</style></head><body>
<h1>{html_lib.escape(title)}</h1>
<p class="note">Generated {html_lib.escape(d.generated)}. Not financial advice. Delayed public data — confirm with your broker.</p>
<p>KSE-100 {html_lib.escape(d.day_label)} {html_lib.escape(fmt_pct(d.day_pct))} at {html_lib.escape(fmt(last_close))}.
15d return {html_lib.escape(r15)}, vs 15-day {html_lib.escape(vs_15)}, vs 200-day {html_lib.escape(vs_200)}.
Mood {html_lib.escape(d.sentiment_label)} ({d.sentiment_score}/100).</p>
<h2>Andaza (kal, 15 din, 200 din)</h2>
<p>{outlook}</p>
<h2>Watchlist</h2>
{html_table(hist_h, hrows(d.watchlist))}
<h2>Quality / study names (not a buy list)</h2>
{html_table(hist_h, hrows(strong))}
<h2>KSE-100 relative up</h2>
{html_table(quote_h, qrows(gainers))}
<h2>KSE-100 relative down</h2>
{html_table(quote_h, qrows(losers))}
<h2>Most traded</h2>
{html_table(quote_h, qrows(active))}
<h2>Firm sectors</h2>
{html_table(sector_h, hot_rows) if hot_rows else "<p>None today.</p>"}
<h2>Soft sectors</h2>
{html_table(sector_h, cold_rows)}
<h2>Headlines</h2>
<ul>{news}</ul>
</body></html>
"""


def multipart_form(fields: dict[str, str], filename: str, content: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = f"----psx{int(time.time() * 1000)}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(content)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def post_telegram_document(token: str, chat_id: str, path: Path, caption: str, label: str) -> None:
    data, content_type = multipart_form(
        {"chat_id": chat_id, "caption": caption[:1024], "disable_content_type_detection": "false"},
        path.name,
        path.read_bytes(),
        "text/html; charset=utf-8",
    )
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=data,
        headers={"User-Agent": USER_AGENT, "Content-Type": content_type},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()
    print(f"Telegram {label}: sent document {path.name}", file=sys.stderr)


def post_telegram(
    parts: list[str],
    token: str,
    chat_id: str,
    label: str,
    document: Path | None = None,
) -> None:
    token, chat_id = token.strip(), chat_id.strip()
    if not token or not chat_id:
        print(f"Telegram {label}: skipped (missing token or chat id)", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i, text in enumerate(parts):
        payload = {
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        if i < len(parts) - 1 or document is not None:
            time.sleep(0.4)
    if document is not None and document.exists():
        post_telegram_document(
            token,
            chat_id,
            document,
            "Tables wali file — watchlist, 15-day, 200-day, movers, sectors. Financial advice nahi.",
            label,
        )
    print(f"Telegram {label}: sent {len(parts)} messages", file=sys.stderr)



def post_webhook(text: str) -> None:
    url = os.environ.get("DIGEST_WEBHOOK_URL", "").strip()
    if not url:
        return
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


def write_telegram_preview(d: Digest) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    def dump(path: Path, parts: list[str]) -> None:
        chunks = [
            f"===== MESSAGE {i}/{len(parts)} ({len(text)} chars) =====\n{text}"
            for i, text in enumerate(parts, 1)
        ]
        path.write_text("\n\n".join(chunks) + "\n", encoding="utf-8")

    dump(REPORTS_DIR / "telegram-learner.txt", learner_telegram_bundle(d))
    dump(REPORTS_DIR / "telegram-investor.txt", investor_telegram_bundle(d))


def write_reports(markdown: str, digest: Digest | None = None) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamped = REPORTS_DIR / f"{date.today().isoformat()}.md"
    latest = REPORTS_DIR / "latest.md"
    learner = markdown
    investor = markdown
    if digest is not None:
        learner = ru_learner_markdown(digest) + "\n\n---\n\n" + markdown
        investor = ru_investor_markdown(digest) + "\n\n---\n\n" + markdown
        save_snapshot(digest)
        day = date.today().isoformat()
        learner_html = REPORTS_DIR / "latest.html"
        investor_html = REPORTS_DIR / "latest-investor.html"
        learner_html.write_text(build_html_report(digest, "learner"), encoding="utf-8")
        investor_html.write_text(build_html_report(digest, "investor"), encoding="utf-8")
        (REPORTS_DIR / f"{day}.html").write_text(learner_html.read_text(encoding="utf-8"), encoding="utf-8")
        (REPORTS_DIR / f"{day}-investor.html").write_text(investor_html.read_text(encoding="utf-8"), encoding="utf-8")
        write_telegram_preview(digest)
    stamped.write_text(learner, encoding="utf-8")
    latest.write_text(learner, encoding="utf-8")
    (REPORTS_DIR / "latest-investor.md").write_text(investor, encoding="utf-8")
    (REPORTS_DIR / f"{date.today().isoformat()}-investor.md").write_text(investor, encoding="utf-8")
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
        digest = collect()
        report = build_report(digest)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(report)
    path = write_reports(report, digest)
    append_github_summary(report)
    try:
        post_telegram(
            learner_telegram_bundle(digest),
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""),
            "learner",
            document=REPORTS_DIR / "latest.html",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Telegram learner skipped: {exc}", file=sys.stderr)
    try:
        post_telegram(
            investor_telegram_bundle(digest),
            os.environ.get("TELEGRAM_BOT_TOKEN_INVESTOR", ""),
            os.environ.get("TELEGRAM_CHAT_ID_INVESTOR", ""),
            "investor",
            document=REPORTS_DIR / "latest-investor.html",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Telegram investor skipped: {exc}", file=sys.stderr)
    try:
        post_webhook(report)
    except Exception as exc:  # noqa: BLE001
        print(f"Webhook skipped: {exc}", file=sys.stderr)
    print(f"\nWrote {path} and {REPORTS_DIR / 'latest.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
