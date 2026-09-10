#!/usr/bin/env python3
"""Free daily PSX digest: sentiment, playbook, and historically strong names.

Uses only the Python standard library and public PSX / news sources.
Personal use only. Not financial advice.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import statistics
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


def http_get(url: str, timeout: int = 25, referer: str | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html, text/xml, */*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def psx_get(path: str) -> bytes:
    return http_get(f"{PSX_ORIGIN}{path}", referer=f"{PSX_ORIGIN}/")


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
                payload = json.loads(http_get(base.format(symbol=encoded)))
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
                except Exception:
                    continue
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
            "INSUFFICIENT HISTORY",
            "Sit tight until a longer series is available.",
            "Need about 200 trading days before a 200-day average read is meaningful.",
        )
    above_200 = price > ma200
    if ma50 is not None and ma50 > ma200 and above_200:
        return (
            "UPTREND",
            "Trend supports long-horizon accumulation — still not a lump-sum green light.",
            "Price and the 50-day average are both above the 200-day average. Rising-market regime.",
        )
    if ma50 is not None and ma50 < ma200 and not above_200:
        return (
            "DOWNTREND",
            "Sit tight, or dollar-cost average only with money you can leave untouched.",
            "Price and the 50-day average are both below the 200-day average. Weakening-market regime.",
        )
    if above_200:
        return (
            "MIXED / LATE TREND",
            "Do not chase. If you invest, keep it small and staggered.",
            "Price is still above the 200-day average, but the shorter trend is not clean. Whipsaws are common.",
        )
    return (
        "MIXED / WEAK",
        "Prefer waiting or tiny staged buys over a lump sum.",
        "Price is under the 200-day average. Until it reclaims that line and holds, bulls have to prove it.",
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
        notes.append(f"Index is up {day_pct:+.2f}% on the session, which lifts near-term mood.")
    elif day_pct < -0.4:
        score -= 12
        notes.append(f"Index is down {day_pct:+.2f}% on the session, which weighs on near-term mood.")
    else:
        notes.append(f"Index is roughly flat ({day_pct:+.2f}%), so today is not a mood-setter on its own.")

    if ma200 is not None:
        if price > ma200:
            score += 10
            notes.append(f"KSE-100 is {pct(price - ma200, ma200):+.1f}% above the 200-day average (longer trend still intact).")
        else:
            score -= 10
            notes.append(f"KSE-100 is {pct(price - ma200, ma200):+.1f}% below the 200-day average (longer trend is damaged).")
    if ma50 is not None and ma200 is not None:
        if ma50 > ma200:
            score += 8
            notes.append("The 50-day average is above the 200-day average (intermediate trend still constructive).")
        else:
            score -= 8
            notes.append("The 50-day average is below the 200-day average (intermediate trend has rolled over).")

    if quotes:
        up = sum(1 for q in quotes if q.pct > 0)
        down = sum(1 for q in quotes if q.pct < 0)
        total = up + down
        if total:
            breadth = (up - down) / total
            score += int(breadth * 14)
            notes.append(f"Market breadth: {up} advancers vs {down} decliners across the regular board.")

    pos_n = sum(1 for h in headlines if h.tone == "positive")
    neg_n = sum(1 for h in headlines if h.tone == "negative")
    score += (pos_n - neg_n) * 2
    notes.append(f"Headline mix in this brief: {pos_n} constructive, {neg_n} cautious, {len(headlines) - pos_n - neg_n} mixed.")

    score = max(5, min(95, score))
    if score >= 72:
        label = "CONSTRUCTIVE / RISK-ON"
    elif score >= 58:
        label = "CAUTIOUSLY POSITIVE"
    elif score >= 45:
        label = "NEUTRAL / MIXED"
    elif score >= 32:
        label = "CAUTIOUS / RISK-OFF"
    else:
        label = "FEARFUL / DEFENSIVE"
    return score, label, notes


def build_playbook(digest_regime: str, day_pct: float, score: int) -> tuple[list[str], list[str]]:
    if digest_regime == "DOWNTREND" or score < 32:
        playbook = [
            "**Do not buy the dip blindly today.** The tape is defensive. Capital preservation beats hero trades.",
            "**Watchlist only** unless you already have a written 3-year thesis on a name.",
            "**If cash is burning a hole:** split any buy into 4 weekly slices. Put at most one slice to work today.",
            "**Avoid** upper-circuit penny names and high-volume junk. Those are trading, not investing.",
        ]
        invest = [
            "Today is a **poor day for lump-sum investing** in the broad market.",
            "If you still want exposure, restrict it to **historically strong KSE-100 names** below (above their 200-day average with positive 6-month and 1-year returns).",
            "Prefer names that are **quiet or slightly red**, not the ones hitting 10% limit-up.",
        ]
    elif digest_regime.startswith("MIXED / WEAK") or score < 45:
        playbook = [
            "**Default action: wait.** The index is below the 200-day average, so the burden of proof is on the bulls.",
            "**Today's job:** read the news, update a watchlist, do not force a full allocation.",
            "**If you invest today:** one small staged buy in a historically strong KSE-100 name, not a basket of movers.",
            "**Do not** chase the day's top percentage gainers. Most of those are not quality compounds.",
        ]
        invest = [
            "Treat today as **optional, small, and quality-only** — not 'go all in'.",
            "Use the **historically strong** table as the only buy universe if you deploy cash.",
            "Skip names that are extended 20%+ above the 50-day average; wait for a pullback.",
        ]
    elif digest_regime.startswith("MIXED"):
        playbook = [
            "**Do not chase.** The long trend is not fully broken, but the tape is messy.",
            "**Today:** hold existing quality, add only on weakness in names you already researched.",
            "Keep dry powder. Whipsaws around the 200-day average chew up impatient money.",
        ]
        invest = [
            "Okay to **nibble** historically strong KSE-100 names if they are not extended.",
            "Still avoid a lump sum until price and the 50-day average are both back above the 200-day.",
        ]
    else:
        playbook = [
            "**Trend is a tailwind**, so long-horizon accumulation is allowed — still stagger buys.",
            "**Today:** add to quality on ordinary red days; do not FOMO into already-extended names.",
            "Rebalance only if a single name is an outsized share of your equity sleeve.",
        ]
        invest = [
            "Historically strong KSE-100 names remain the core universe.",
            "A green market is not permission to buy illiquid circuit-hitters.",
        ]
    if abs(day_pct) >= 1.5:
        playbook.append(
            f"**Wide day ({day_pct:+.2f}%).** Single-session moves this large are noise plus emotion. Sleep on any new idea."
        )
    return playbook, invest


def strong_names(histories: list[StockHistory]) -> list[StockHistory]:
    eligible = [
        h for h in histories
        if h.above_200 and (h.r6m or 0) > 0 and (h.r1y or 0) > 0
    ]
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


def index_change(series: QuoteSeries) -> tuple[str, float, float, float | None, float | None, float | None, float | None]:
    closes = [c for _, c in series.closes]
    last_day, last_close = series.last
    eod_day, eod_close = series.closes[-1]
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
    index = load_index("KSE100")
    kse30 = load_optional_index("KSE30")
    allshr = load_optional_index("ALLSHR")
    quotes = fetch_market_watch()
    kse100 = fetch_kse100_board()
    sectors = fetch_sectors()
    attach_sectors(quotes, kse100, sectors)
    headlines = fetch_headlines(12)
    label, day_change, day_pct, five_pct, ma20, ma50, ma200 = index_change(index)
    last_price = index.last[1]
    regime, stance, why = classify_regime(last_price, ma50, ma200)
    score, sent_label, sent_notes = score_sentiment(day_pct, last_price, ma50, ma200, quotes, headlines)
    playbook, invest_today = build_playbook(regime, day_pct, score)
    market_day = index.closes[-1][0]
    print(f"Fetching 1-year history for {len(kse100)} KSE-100 names...", file=sys.stderr)
    histories = fetch_histories(kse100, market_day)
    return Digest(
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
    )


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
    avoid = "\n".join(
        f"- `{q.symbol}` ({q.sector or 'n/a'}) {fmt_pct(q.pct)} — not KSE-100, treat as speculation"
        for q in junk
    ) or "- No extreme non-index circuit names stood out."
    weak_s = "\n".join(
        f"- `{h.symbol}` {h.name[:40]} · 1y {fmt_pct(h.r1y)} · still below 200-day average"
        for h in weak_hist
    ) or "- None flagged."

    kse30 = index_line(d.kse30, "KSE-30 n/a")
    allshr = index_line(d.allshr, "ALLSHR n/a")

    return f"""# Daily PSX digest — {last_day.isoformat()}

**Generated:** {d.generated}  
**Source:** {d.index.source or "PSX data portal"} · delayed public data · personal use only  
**This is not financial advice.** Equities can lose value. One session is not a trend.

---

## What you should do today

**Regime:** {d.regime}  
**Sentiment:** {d.sentiment_label} ({d.sentiment_score}/100)  
**Default stance:** {d.stance}

{playbook}

---

## Executive snapshot

| | |
| --- | --- |
| KSE-100 | **{fmt(last_close)}** |
| Day | **{d.day_label}** {signed(d.day_change)} ({fmt_pct(d.day_pct)}) |
| 5 sessions | {fmt_pct(d.five_pct)} |
| 20-day average | {fmt(d.ma20)} |
| 50-day average | {fmt(d.ma50)} |
| 200-day average | {fmt(d.ma200)} |
| vs 200-day | {vs_200} |
| 20-day volatility (stdev) | {vol20} pts |

- {kse30}
- {allshr}

{d.why}

---

## Sentiment detail

{sentiment}

Breadth and news can disagree with the index. If they all point the same way, the mood is more trustworthy. If they split, size down.

---

## If you want to invest today

{invest}

### Historically strong KSE-100 names
These cleared a **rules filter**, not a crystal ball: still above the 200-day average, and positive over ~6 months and ~1 year. Ranked by 1-year return.

{md_table(
    ["Symbol", "Name", "Last", "Day", "1m", "3m", "6m", "1y", "vs 200d"],
    hrows(strong),
)}

### Better entries vs already-extended
Names from that list **not stretched** (>12% above the 50-day average are treated as extended):

{md_table(
    ["Symbol", "Name", "Last", "Day", "1m", "3m", "6m", "1y", "vs 200d"],
    hrows(buyable),
)}

{"Extended / wait for a pullback: " + ", ".join(f"`{h.symbol}`" for h in extended) if extended else "No names on the strong list look violently extended versus the 50-day average."}

### Leave these alone today
KSE-100 names **below** the 200-day average (weak 1-year first):

{weak_s}

Hot non-index names (easy to get trapped):

{avoid}

---

## Today's KSE-100 tape

### Gainers
{md_table(["Symbol", "Name", "Sector", "Last", "Day", "Volume"], qrows(gainers))}

### Losers
{md_table(["Symbol", "Name", "Sector", "Last", "Day", "Volume"], qrows(losers))}

### Most active
{md_table(["Symbol", "Name", "Sector", "Last", "Day", "Volume"], qrows(active))}

---

## Sector breadth

Advance minus decline. Positive = more stocks up than down in that sector.

### Firm
{md_table(["Sector", "Adv", "Dec", "Unch", "Turnover", "Mcap (B)"],
    [[s.name, str(s.advance), str(s.decline), str(s.unchanged), f"{s.turnover:,.0f}", fmt(s.mcap)] for s in hot_sectors]) if hot_sectors else "_No sector has more advancers than decliners today._"}

### Soft
{md_table(["Sector", "Adv", "Dec", "Unch", "Turnover", "Mcap (B)"],
    [[s.name, str(s.advance), str(s.decline), str(s.unchanged), f"{s.turnover:,.0f}", fmt(s.mcap)] for s in cold_sectors])}

---

## News & tone

🟢 constructive · 🔴 cautious · ⚪ mixed. Tone is a keyword read of the headline, not a full article score.

{chr(10).join(news_lines) if news_lines else "- No headlines could be fetched today."}

---

## How to use this

1. Read **What you should do today** first. If it says wait, the tables are a watchlist, not a shopping list.
2. Historically strong ≠ cheap. Check filings, debt, payouts, and your own time horizon.
3. Size so a 25% drawdown on a name does not change your life.
4. This file is delayed public data. Confirm last price with your broker before any order.
"""


def build_telegram_parts(d: Digest) -> list[str]:
    last_day, last_close = d.index.last
    vs_200 = "n/a" if d.ma200 is None else fmt_pct(pct(last_close - d.ma200, d.ma200))
    url = os.environ.get(
        "DIGEST_REPORT_URL",
        "https://github.com/AbuzarGhazanfarKhan/psx-daily-digest/blob/main/reports/latest.md",
    )
    strong = strong_names(d.histories)[:8]
    buyable = [h for h in strong if (h.vs_50 or 0) <= 12][:6]
    kse_quotes = [q for q in (d.kse100 or [q for q in d.quotes if "KSE100" in q.listed]) if not q.symbol.endswith("XD")]
    gainers = top(kse_quotes, key=lambda q: q.pct, n=5)
    losers = top(kse_quotes, key=lambda q: q.pct, reverse=False, n=5)

    def lines(items: list[StockHistory]) -> str:
        out = []
        for h in items:
            out.append(
                f"{h.symbol}  1y {fmt_pct(h.r1y)}  6m {fmt_pct(h.r6m)}  1m {fmt_pct(h.r1m)}  day {fmt_pct(h.day_pct)}"
            )
        return "\n".join(out) or "None passed the filter today."

    part1 = f"""PSX DAILY  {d.generated}
NOT FINANCIAL ADVICE. You can lose money.

KSE-100  {fmt(last_close)}
Day      {d.day_label}  {signed(d.day_change)} ({fmt_pct(d.day_pct)})
5d       {fmt_pct(d.five_pct)}
vs 200d  {vs_200}

REGIME     {d.regime}
SENTIMENT  {d.sentiment_label}  ({d.sentiment_score}/100)
STANCE     {d.stance}

WHAT YOU SHOULD DO TODAY
""" + "\n".join(f"{i}. {strip_md(p)}" for i, p in enumerate(d.playbook, 1))

    part2 = "IF YOU WANT TO INVEST TODAY\n" + "\n".join(f"• {strip_md(p)}" for p in d.invest_today)
    part2 += "\n\nHISTORICALLY STRONG KSE-100 (1y / 6m / 1m / day)\n" + lines(strong)
    part2 += "\n\nBETTER ENTRIES (not stretched vs 50d)\n" + lines(buyable)

    part3 = "TODAY'S KSE-100 MOVERS\nGainers:\n" + "\n".join(
        f"▲ {q.symbol} {fmt_pct(q.pct)}" for q in gainers
    )
    part3 += "\nLosers:\n" + "\n".join(f"▼ {q.symbol} {fmt_pct(q.pct)}" for q in losers)
    part3 += "\n\nNEWS\n"
    for item in d.headlines[:6]:
        mark = {"positive": "+", "negative": "-", "neutral": "·"}[item.tone]
        part3 += f"{mark} {item.title}\n"
    part3 += f"\nFull formatted report:\n{url}"
    return [part1, part2, part3]


def strip_md(text: str) -> str:
    return text.replace("**", "").replace("`", "")


def post_telegram(parts: list[str]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
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
        digest = collect()
        report = build_report(digest)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(report)
    path = write_reports(report)
    append_github_summary(report)
    try:
        post_telegram(build_telegram_parts(digest))
    except Exception as exc:  # noqa: BLE001
        print(f"Telegram skipped: {exc}", file=sys.stderr)
    try:
        post_webhook(report)
    except Exception as exc:  # noqa: BLE001
        print(f"Webhook skipped: {exc}", file=sys.stderr)
    print(f"\nWrote {path} and {REPORTS_DIR / 'latest.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
