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
    r1m: float | None
    r3m: float | None
    r6m: float | None
    r1y: float | None
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
        ma50 = moving_average(closes, 50)
        ma200 = moving_average(closes, 200)
        quote = by_symbol[symbol]
        return StockHistory(
            symbol=symbol,
            name=quote.name,
            sector=quote.sector,
            last=last_close,
            day_pct=quote.pct,
            r1m=ret(closes, 21),
            r3m=ret(closes, 63),
            r6m=ret(closes, 126),
            r1y=ret(closes, 252),
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
        "**Learn three things:** (1) did the whole market go up or down? (2) is KSE-100 above or below the 200-day line? (3) did *most* stocks move with it, or only a few?",
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
    vs = "" if ma200 is None else f" | vs 200d {fmt_pct(pct(series.last[1] - ma200, ma200))}"
    five_s = "" if five is None else f" | 5d {fmt_pct(five)}"
    return f"{series.symbol} **{fmt(series.last[1])}** · {label} {signed(change)} ({fmt_pct(day_pct)}){five_s}{vs}"


def top(items: list, key, reverse: bool = True, n: int = 8):
    return sorted(items, key=key, reverse=reverse)[:n]


def build_report(d: Digest) -> str:
    last_day, last_close = d.index.last
    vs_200 = "n/a" if d.ma200 is None else fmt_pct(pct(last_close - d.ma200, d.ma200))
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
            fmt_pct(h.r1m),
            fmt_pct(h.r3m),
            fmt_pct(h.r6m),
            fmt_pct(h.r1y),
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

If the index, the 200-day line, most stocks, *and* the news all agree, trust the mood more. If they argue, just watch.

---

## Your watchlist

These are **study names** from `watchlist.txt` (big, commonly discussed companies). Edit that file anytime. This is not a buy list.

{md_table(["Symbol", "Name", "Last", "Day", "1m", "3m", "6m", "1y", "vs 200d"], watch_rows) if watch_rows else "_Could not load watchlist prices today._"}

How to read a row: **Day** = today. **1m / 6m / 1y** = last month / ~6 months / ~1 year. **vs 200d** = above (+) or below (−) the slow trend line.

---

## Healthier long-term KSE-100 names (study list)

Filter (not magic): still above the 200-day line, up over ~6 months and ~1 year, **not** a crazy rocket (+80% in a year is excluded), and big/liquid enough for a beginner to learn on.

{invest}

{md_table(["Symbol", "Name", "Last", "Day", "1m", "3m", "6m", "1y", "vs 200d"], hrows(strong))}

Quieter of those (not running too far ahead of the 50-day line):

{md_table(["Symbol", "Name", "Last", "Day", "1m", "3m", "6m", "1y", "vs 200d"], hrows(buyable))}

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
- **200-day average** — slow trend line. Above it ≈ long uptrend; below it ≈ long downtrend.
- **50-day average** — faster trend line.
- **Breadth** — how many stocks went up vs down, not just the index.
- **Volume** — how many shares traded. Very low volume + huge % move is often a trap.
- **Watchlist** — names you track without buying yet.

## How to use these notes for a few weeks

1. Read **If you have not bought anything yet** first. Do not open a buy order because a table looks green.
2. Each day, say out loud: up or down? above or below 200-day? scared or hopeful news?
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
        "gainers": top(kse_quotes, key=lambda q: q.pct, n=5),
        "losers": top(kse_quotes, key=lambda q: q.pct, reverse=False, n=5),
        "strong": strong,
        "active": top(kse_quotes, key=lambda q: q.volume, n=5),
        "hot_sectors": [s for s in top(d.sectors, key=lambda s: s.advance - s.decline, n=6) if s.advance > s.decline][:5],
        "cold_sectors": top(d.sectors, key=lambda s: s.advance - s.decline, reverse=False, n=5),
    }


def ru_vs_200(vs: float | None) -> str:
    if vs is None:
        return "200-day average ka data available nahi"
    if vs >= 0:
        return (
            f"KSE-100 is waqt 200-day average se {fmt_pct(vs)} UPAR hai. "
            "Matlab last ~200 trading days ki average se price oonchi hai, jo long uptrend ki taraf ishara hai."
        )
    return (
        f"KSE-100 is waqt 200-day average se {abs(vs):.2f}% NEECHE hai. "
        "Matlab last ~200 trading days ki average se price neeche hai, is liye long uptrend prove nahi hua."
    )


def ru_lesson(d: Digest) -> str:
    if abs(d.day_pct) >= 1.5:
        return (
            f"Aaj ka move {fmt_pct(d.day_pct)} hai, jo ehsas mein bohat bara lagta hai. "
            "Long-term ke liye yeh sirf ek trading day hai. Aise din headlines bhi tez ho jati hain; "
            "unhe mood samajhein, buy signal nahi."
        )
    if d.ma200 is not None and d.index.last[1] < d.ma200:
        return (
            "Jab price 200-day average ke neeche ho, market ko wapas uptrend ke liye "
            "pehle yeh line cross karni padti hai aur uske upar tikna padta hai. Us se pehle wait valid hai."
        )
    t = _tape(d)
    if t["down"] > max(t["up"], 1) * 3:
        return (
            "Aaj breadth weak thi: index ek number hai, lekin zyada stocks down the. "
            "Is ka matlab pressure sirf ek-do bari companies tak mahdood nahi tha."
        )
    return (
        "Roz yeh teen sawal karein: market up hai ya down? 200-day average ke upar hai ya neeche? "
        "Headlines hopeful hain ya worried? Jawab likh lena hi learning hai."
    )


def learner_english_short(d: Digest) -> str:
    t = _tape(d)
    vs = "n/a" if t["vs_200"] is None else fmt_pct(t["vs_200"])
    return (
        f"LEARNER — English summary  {d.generated}\n"
        f"KSE-100 (Pakistan's 100 larger listed companies): {d.day_label} {fmt_pct(d.day_pct)} at {fmt(t['last'])}.\n"
        f"Versus 200-day average (slow trend line of ~200 sessions): {vs}. "
        f"Sentiment {d.sentiment_label} ({d.sentiment_score}/100).\n"
        f"Action: do not place a buy order. You are in a learning period. "
        f"The next messages explain every term in Roman Urdu.\n"
        f"This is not financial advice."
    )


def investor_english_short(d: Digest) -> str:
    t = _tape(d)
    vs = "n/a" if t["vs_200"] is None else fmt_pct(t["vs_200"])
    if d.regime.startswith("DOWN") or d.sentiment_score < 32:
        bias = "Stance: defensive. Avoid chase and lump-sum deployment."
    elif "below" in d.regime.lower() or d.sentiment_score < 45:
        bias = "Stance: wait. If adding, use small staged size in liquid KSE-100 names only."
    elif d.regime.startswith("UP"):
        bias = "Stance: long trend supportive; do not chase names extended above the 50-day average."
    else:
        bias = "Stance: mixed tape. Keep position size conservative."
    return (
        f"INVESTOR — English summary  {d.generated}\n"
        f"KSE-100 {d.day_label} {fmt_pct(d.day_pct)} | {fmt(t['last'])} | vs 200-day average {vs}\n"
        f"Breadth (stocks up vs down): {t['up']} / {t['down']} | sentiment {d.sentiment_score}/100 | {d.regime}\n"
        f"{bias}\n"
        f"Detailed Roman Urdu follows. This is not financial advice."
    )


def investor_ru_stance(d: Digest) -> list[str]:
    if d.regime.startswith("DOWN") or d.sentiment_score < 32:
        return [
            "Bias defensive rakhein. Decline ko automatic discount na samjhein.",
            "Lump-sum (ek hi dafa bari raqam) aaj munasiib nahi. Agar add karein to chhoti size, aur sirf us company par jiska 3-year thesis pehle se maujood ho.",
            "Limit-up (~10% daily ceiling) aur low-volume spikes speculation hain; core portfolio ke liye nahi.",
        ]
    if "below" in d.regime.lower() or d.sentiment_score < 45:
        return [
            "KSE-100 200-day average ke neeche hai, is liye long bull case confirm nahi.",
            "Default wait. Staged add (chand dafa chhoti khareed) sirf liquid KSE-100 names mein sochiye, circuit-hitter mein nahi.",
            "Jo name 50-day average se ziyada stretch ho chuka ho, usko chase na karein.",
        ]
    if d.regime.startswith("UP"):
        return [
            "Long trend supportive hai; lekin green session ka matlab har name buy nahi.",
            "Quality names par ordinary red days pe add consider ho sakta hai. Extended names par FOMO avoid karein.",
            "Agar ek stock portfolio ka disproportionate hissa ban jaye to rebalance par ghaur karein.",
        ]
    return [
        "Tape mixed hai. Bina plan ke hero trade se parhez karein.",
        "200-day average ke qareeb whipsaw (tez up-down) aam hai — size conservative rakhein.",
        "Jab tak price aur 50-day average dono 200-day ke upar na hon, dry powder (unused cash) rakhein.",
    ]


def learner_ru_parts(d: Digest) -> list[str]:
    t = _tape(d)
    vs = t["vs_200"]
    closed = f"Note: {d.closed_note}\n\n" if d.closed_note else ""
    watch = "\n".join(
        f"• {h.symbol} ({h.name[:32]})\n"
        f"  Last price {fmt(h.last)} | aaj {fmt_pct(h.day_pct)} | "
        f"1 month {fmt_pct(h.r1m)} | 6 months {fmt_pct(h.r6m)} | 1 year {fmt_pct(h.r1y)}\n"
        f"  vs 200-day average: {fmt_pct(h.vs_200)} "
        f"({'upar = long chart theek' if (h.vs_200 or 0) >= 0 else 'neeche = long chart kamzor'})"
        for h in d.watchlist
    ) or "Watchlist load nahi ho saki."
    p1 = (
        f"LEARNER — Roman Urdu tafseel  {d.generated}\n"
        f"Yeh report taleem ke liye hai, financial advice nahi. Shares mein loss mumkin hai.\n"
        f"{closed}"
        f"1) Index kya hai?\n"
        f"KSE-100 Pakistan Stock Exchange ki 100 bari companies ka scoreboard hai. "
        f"Aaj yeh {d.day_label} raha: {fmt_pct(d.day_pct)}, level {fmt(t['last'])}. "
        f"Agar KSE-100 gire to aksar portfolios mein ehsas hota hai, chahe aap ne abhi kuch khareeda na ho.\n\n"
        f"2) 200-day average kya hai?\n"
        f"Yeh pichli ~200 trading days ki average price ki ahista line hai. "
        f"Upar hona = lambi muddat ka chart generally up; neeche hona = long trend kamzor. "
        f"{ru_vs_200(vs)}\n\n"
        f"3) Breadth kya hai?\n"
        f"Yeh ginti hai ke kitne stocks up gaye, kitne down. Aaj {t['up']} up, {t['down']} down. "
        f"Agar index thora move kare lekin zyada stocks down hon, to pressure chhupa hua hota hai.\n\n"
        f"4) Sentiment score kya hai?\n"
        f"0 = bohat dara hua mood, 100 = bohat hopeful. Aaj {d.sentiment_score}/100 ({d.sentiment_label}). "
        f"Yeh andaza index, 200-day average, breadth, aur headlines se banaya jata hai — guarantee nahi.\n\n"
        f"Aap ka account naya hai. Aaj koi buy order na dein. Cash broker mein reh sakti hai. "
        f"Pehle 2–3 weeks yeh notes parh kar terms seekhein.\n\n"
        f"Aaj ki dars: {ru_lesson(d)}"
    )
    p2 = (
        "Watchlist kya hai?\n"
        "Yeh un companies ki list hai jinhein aap bina khareede track karte hain (watchlist.txt). "
        "Buy list nahi hai.\n\n"
        "Column ka matlab:\n"
        "• day = aaj ka change\n"
        "• 1 month / 6 months / 1 year = us daur ka return\n"
        "• vs 200-day average = slow trend se kitna upar/neeche\n\n"
        f"{watch}\n\n"
        "Pichli report se farq:\n"
        + "\n".join(f"• {strip_md(x)}" for x in d.vs_yesterday)
    )
    p3 = (
        "KSE-100 ke andar aaj ke movers (seekhne ke liye, khareedne ke liye nahi):\n"
        "Relative up: "
        + ", ".join(f"{q.symbol} {fmt_pct(q.pct)}" for q in t["gainers"])
        + "\nRelative down: "
        + ", ".join(f"{q.symbol} {fmt_pct(q.pct)}" for q in t["losers"])
        + "\n\nVolume = kitne shares trade hue. Bohat kam volume + tez % move aksar unreliable hota hai.\n"
        "Limit-up (~+10%) wale chhote, non-KSE-100 names beginner ke liye trap ho sakte hain; unhe ignore karein.\n\n"
        "Headlines (skim; + hopeful, - worried, · mixed):\n"
    )
    for item in d.headlines[:6]:
        mark = {"positive": "+", "negative": "-", "neutral": "·"}[item.tone]
        p3 += f"{mark} {item.title}\n"
    p3 += "\nMukammal tables: GitHub file reports/latest.md"
    return [p1, p2, p3]


def investor_ru_parts(d: Digest) -> list[str]:
    t = _tape(d)
    vs = t["vs_200"]
    closed = f"Note: {d.closed_note}\n\n" if d.closed_note else ""
    stance = "\n".join(f"{i}. {line}" for i, line in enumerate(investor_ru_stance(d), 1))
    strong = t["strong"]
    strong_txt = "\n".join(
        f"• {h.symbol} ({h.name[:28]})\n"
        f"  Last {fmt(h.last)} | session {fmt_pct(h.day_pct)} | "
        f"1m {fmt_pct(h.r1m)} | 6m {fmt_pct(h.r6m)} | 1y {fmt_pct(h.r1y)}\n"
        f"  vs 200-day {fmt_pct(h.vs_200)} | vs 50-day {fmt_pct(h.vs_50)} | volume {h.volume:,.0f}"
        for h in strong
    ) or "Is filter se koi name qualify nahi kiya."
    wide = ""
    if abs(d.day_pct) >= 1.5:
        wide = (
            f"\n\nWide session ({fmt_pct(d.day_pct)}): ek din ka bara move aksar emotion + positioning hota hai. "
            "Nayi aggressive position ko overnight sochna behtar hai."
        )
    p1 = (
        f"INVESTOR — Roman Urdu tafseel  {d.generated}\n"
        f"Yeh research note hai, recommendation nahi. Capital at risk rehta hai.\n"
        f"{closed}"
        f"Tape (live/delayed board ka khulasa):\n"
        f"KSE-100 {d.day_label} {fmt_pct(d.day_pct)}, level {fmt(t['last'])}, "
        f"pichli 5 sessions {fmt_pct(d.five_pct)}.\n"
        f"{ru_vs_200(vs)}\n"
        f"50-day average (tez trend line, ~50 sessions) vs 200-day average (ahista line): "
        f"{'50-day abhi 200-day ke upar hai (intermediate trend itna bura nahi).' if (d.ma50 and d.ma200 and d.ma50 > d.ma200) else '50-day 200-day ke qareeb/neeche hai — intermediate trend kamzor.'}\n\n"
        f"Breadth = kitne symbols up vs down. Aaj {t['up']} up, {t['down']} down. "
        f"Agar decliners heavy hon to index ka koi bounce short-covering (short positions band karna) ho sakta hai, naya accumulation nahi.\n"
        f"Sentiment {d.sentiment_score}/100 ({d.sentiment_label}) — yeh index + trend lines + breadth + headlines ka combined andaza hai.\n"
        f"Regime label: {d.regime}.\n\n"
        f"Stance:\n{stance}{wide}"
    )
    p2 = (
        "Pichli report se farq:\n"
        + "\n".join(f"• {strip_md(x)}" for x in d.vs_yesterday)
        + "\n\nQuality screen kya hai?\n"
        "Yeh buy list nahi. Filter yeh hai: (a) KSE-100 member, (b) price 200-day average ke upar, "
        "(c) ~6-month aur ~1-year return positive, (d) 1-year rocket (~80%+) exclude, "
        "(e) volume/mcap itna ke naam liquid ho. Matlab: lambi chart theek, lekin valuation check aap khud karein.\n\n"
        f"{strong_txt}"
    )
    p3 = (
        "Session tape — KSE-100:\n"
        "Relative gainers: "
        + ", ".join(f"{q.symbol} {fmt_pct(q.pct)}" for q in t["gainers"])
        + "\nRelative losers: "
        + ", ".join(f"{q.symbol} {fmt_pct(q.pct)}" for q in t["losers"])
        + "\nVolume leaders (zyada shares trade): "
        + ", ".join(q.symbol for q in t["active"])
    )
    if t["hot_sectors"]:
        p3 += "\nRelative firm sectors (us group mein ziyada stocks up): " + ", ".join(s.name for s in t["hot_sectors"])
    if t["cold_sectors"]:
        p3 += "\nSoft sectors (zyada stocks down): " + ", ".join(s.name for s in t["cold_sectors"])
    p3 += (
        "\n\nGainer chase vs quality: aaj ka % leader aksar mean-reversion karta hai. "
        "Core add ke liye pehle liquidity, 200-day average, aur khud ka thesis dekhein.\n\n"
        "Headlines (+ hopeful, - worried, · mixed):\n"
    )
    for item in d.headlines[:7]:
        mark = {"positive": "+", "negative": "-", "neutral": "·"}[item.tone]
        p3 += f"{mark} {item.title}\n"
    p3 += "\nMukammal tables: reports/latest-investor.md"
    return [p1, p2, p3]


def learner_telegram_bundle(d: Digest) -> list[str]:
    return [learner_english_short(d), *learner_ru_parts(d)]


def investor_telegram_bundle(d: Digest) -> list[str]:
    return [investor_english_short(d), *investor_ru_parts(d)]


def ru_learner_markdown(d: Digest) -> str:
    return "\n\n".join(["## Roman Urdu (detail)", *learner_ru_parts(d)])


def ru_investor_markdown(d: Digest) -> str:
    t = _tape(d)
    stance = "\n".join(f"- {line}" for line in investor_ru_stance(d))
    body = "\n\n".join(investor_ru_parts(d))
    return (
        f"# Investor note — {d.index.last[0].isoformat()}\n\n"
        f"**Short EN:** KSE-100 {d.day_label} {fmt_pct(d.day_pct)}, sentiment {d.sentiment_score}/100, {d.regime}. "
        f"Not financial advice.\n\n"
        f"## Stance\n{stance}\n\n"
        f"## Roman Urdu (detail)\n\n{body}\n"
    )


def post_telegram(parts: list[str], token: str, chat_id: str, label: str) -> None:
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
        if i < len(parts) - 1:
            time.sleep(0.4)
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


def write_reports(markdown: str, digest: Digest | None = None) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamped = REPORTS_DIR / f"{date.today().isoformat()}.md"
    latest = REPORTS_DIR / "latest.md"
    learner = markdown
    investor = markdown
    if digest is not None:
        learner = (
            learner_english_short(digest)
            + "\n\n"
            + ru_learner_markdown(digest)
            + "\n\n---\n\n"
            + markdown
        )
        investor = ru_investor_markdown(digest)
        save_snapshot(digest)
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
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Telegram learner skipped: {exc}", file=sys.stderr)
    try:
        post_telegram(
            investor_telegram_bundle(digest),
            os.environ.get("TELEGRAM_BOT_TOKEN_INVESTOR", ""),
            os.environ.get("TELEGRAM_CHAT_ID_INVESTOR", ""),
            "investor",
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
