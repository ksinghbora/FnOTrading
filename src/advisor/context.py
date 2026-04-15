"""Fetch external market context for tomorrow's trading session.

Sources (all free, no API keys):
  1. Google News RSS — NIFTY/NSE headlines (existing)
  2. NSE FII/DII API — institutional flows (existing)
  3. Economic calendar — manual JSON (existing)
  4. Yahoo Finance — US markets (S&P 500, NASDAQ, VIX, crude, DXY)
  5. NSE Market Status — GIFT Nifty pre-market futures
  6. MarketWatch RSS — global market headlines
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from src.advisor.models import (
    CalendarEvent,
    ExternalContext,
    FiiDiiData,
    GlobalMarketData,
)

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0  # seconds per external request

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/html, application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Google News RSS ──────────────────────────────────────────────

async def _fetch_news_headlines(max_headlines: int = 10) -> list[str]:
    """Fetch recent NIFTY/NSE market news from Google News RSS."""
    url = (
        "https://news.google.com/rss/search"
        "?q=NIFTY+NSE+India+market&hl=en-IN&gl=IN&ceid=IN:en"
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        root = ET.fromstring(resp.text)
        headlines: list[str] = []
        now = datetime.now()

        for item in root.iter("item"):
            title = item.findtext("title", "")
            pub_date = item.findtext("pubDate", "")
            if not title:
                continue

            # Filter for last 72 hours (wider window for weekends)
            try:
                pub = datetime.strptime(pub_date[:25], "%a, %d %b %Y %H:%M:%S")
                if (now - pub).total_seconds() > 72 * 3600:
                    continue
            except (ValueError, TypeError):
                pass  # Include if date unparseable

            headlines.append(title.strip())
            if len(headlines) >= max_headlines:
                break

        return headlines
    except Exception as e:
        logger.warning(f"[ADVISOR] News fetch failed: {e}")
        return []


# ── Global Market Headlines (MarketWatch RSS) ────────────────────

async def _fetch_global_headlines(max_headlines: int = 8) -> list[str]:
    """Fetch global market headlines from MarketWatch and Bloomberg RSS."""
    feeds = [
        "https://feeds.marketwatch.com/marketwatch/topstories",
        "https://feeds.bloomberg.com/markets/news.rss",
    ]
    headlines: list[str] = []

    for url in feeds:
        if len(headlines) >= max_headlines:
            break
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers={"User-Agent": _HEADERS["User-Agent"]})
                resp.raise_for_status()

            root = ET.fromstring(resp.text)
            now = datetime.now()

            for item in root.iter("item"):
                title = item.findtext("title", "")
                if not title:
                    continue

                # Filter recent only
                pub_date = item.findtext("pubDate", "")
                try:
                    pub = datetime.strptime(pub_date[:25], "%a, %d %b %Y %H:%M:%S")
                    if (now - pub).total_seconds() > 24 * 3600:
                        continue
                except (ValueError, TypeError):
                    pass

                headlines.append(title.strip())
                if len(headlines) >= max_headlines:
                    break
        except Exception as e:
            logger.debug(f"[ADVISOR] Global headline feed failed ({url}): {e}")

    return headlines


# ── FII/DII Data from NSE ───────────────────────────────────────

async def _fetch_fii_dii() -> FiiDiiData | None:
    """Fetch FII/DII trading data from NSE API."""
    url = "https://www.nseindia.com/api/fiidiiTradeReact"
    headers = {
        **_HEADERS,
        "Referer": "https://www.nseindia.com/reports-indices",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            # NSE requires a session — first hit the main page
            await client.get("https://www.nseindia.com", headers=headers)
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()

        data = resp.json()
        # NSE returns list of dicts with category, buyValue, sellValue
        fii_net = 0.0
        dii_net = 0.0
        for entry in data:
            category = entry.get("category", "")
            buy = float(entry.get("buyValue", 0))
            sell = float(entry.get("sellValue", 0))
            if "FII" in category or "FPI" in category:
                fii_net += buy - sell
            elif "DII" in category:
                dii_net += buy - sell

        return FiiDiiData(fii_net=round(fii_net, 2), dii_net=round(dii_net, 2))
    except Exception as e:
        logger.warning(f"[ADVISOR] FII/DII fetch failed (NSE often blocks): {e}")
        return None


# ── US Markets via Yahoo Finance ─────────────────────────────────

async def _fetch_yahoo_quote(symbol: str, client: httpx.AsyncClient) -> dict | None:
    """Fetch a single quote from Yahoo Finance v8 API."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "2d", "interval": "1d"}
    headers = {"User-Agent": _HEADERS["User-Agent"]}

    try:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        meta = result[0].get("meta", {})
        closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])

        if len(closes) >= 2 and closes[-1] and closes[-2]:
            prev = closes[-2]
            last = closes[-1]
            change_pct = (last - prev) / prev * 100
            return {"close": round(last, 2), "change_pct": round(change_pct, 2)}
        elif len(closes) >= 1 and closes[-1]:
            return {
                "close": round(closes[-1], 2),
                "change_pct": round(meta.get("regularMarketChangePercent", 0), 2),
            }
        return None
    except Exception as e:
        logger.debug(f"[ADVISOR] Yahoo quote failed for {symbol}: {e}")
        return None


async def _fetch_global_markets() -> GlobalMarketData:
    """Fetch US markets, VIX, crude oil, DXY from Yahoo Finance."""
    symbols = {
        "sp500": "^GSPC",
        "nasdaq": "^IXIC",
        "us_vix": "^VIX",
        "crude": "BZ=F",   # Brent crude futures
        "dxy": "DX-Y.NYB",  # US Dollar Index
    }

    results: dict[str, dict] = {}

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for key, symbol in symbols.items():
            quote = await _fetch_yahoo_quote(symbol, client)
            if quote:
                results[key] = quote

    # Derive global sentiment
    sp_chg = results.get("sp500", {}).get("change_pct", 0)
    nas_chg = results.get("nasdaq", {}).get("change_pct", 0)
    vix_val = results.get("us_vix", {}).get("close", 0)
    avg_chg = (sp_chg + nas_chg) / 2 if sp_chg and nas_chg else sp_chg or nas_chg

    if avg_chg > 0.5 and vix_val < 20:
        sentiment = "risk_on"
    elif avg_chg < -0.5 or vix_val > 25:
        sentiment = "risk_off"
    elif abs(avg_chg) <= 0.5:
        sentiment = "mixed"
    else:
        sentiment = ""

    return GlobalMarketData(
        sp500_close=results.get("sp500", {}).get("close", 0),
        sp500_change_pct=results.get("sp500", {}).get("change_pct", 0),
        nasdaq_close=results.get("nasdaq", {}).get("close", 0),
        nasdaq_change_pct=results.get("nasdaq", {}).get("change_pct", 0),
        us_vix=results.get("us_vix", {}).get("close", 0),
        us_vix_change_pct=results.get("us_vix", {}).get("change_pct", 0),
        crude_oil_close=results.get("crude", {}).get("close", 0),
        crude_oil_change_pct=results.get("crude", {}).get("change_pct", 0),
        dxy=results.get("dxy", {}).get("close", 0),
        dxy_change_pct=results.get("dxy", {}).get("change_pct", 0),
        global_sentiment=sentiment,
    )


# ── GIFT Nifty (NSE Market Status API) ──────────────────────────

async def _fetch_gift_nifty(nifty_prev_close: float = 0) -> tuple[float, float]:
    """Fetch GIFT Nifty pre-market futures or indicative NIFTY from NSE.

    GIFT Nifty is available only during pre-market (~7:30-9:15 AM IST).
    After hours, returns 0 (not available).

    Returns (gift_nifty_price, change_pct_vs_nifty_close).
    """
    url = "https://www.nseindia.com/api/marketStatus"
    headers = {
        **_HEADERS,
        "Referer": "https://www.nseindia.com",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            await client.get("https://www.nseindia.com", headers=headers)
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()

        data = resp.json()
        gift_price = 0.0

        # 1. Try indicativeNifty50 (pre-market GIFT Nifty)
        indicative = data.get("indicativenifty50", {})
        if isinstance(indicative, dict):
            idx_last = indicative.get("indexLast")
            if idx_last is not None:
                try:
                    gift_price = float(str(idx_last).replace(",", ""))
                except (ValueError, TypeError):
                    pass

        # 2. Try marketState entries for GIFT/SGX
        if gift_price <= 0:
            for market in data.get("marketState", []):
                market_name = str(market.get("market", "")).lower()
                if "gift" in market_name or "sgx" in market_name:
                    try:
                        last = market.get("last", 0)
                        val = float(str(last).replace(",", ""))
                        if val > 10000:
                            gift_price = val
                            break
                    except (ValueError, TypeError):
                        continue

        change_pct = 0.0
        if gift_price > 0 and nifty_prev_close > 0:
            change_pct = round((gift_price - nifty_prev_close) / nifty_prev_close * 100, 2)

        return gift_price, change_pct
    except Exception as e:
        logger.warning(f"[ADVISOR] GIFT Nifty fetch failed: {e}")
        return 0.0, 0.0


# ── Economic Calendar ────────────────────────────────────────────

def _load_economic_calendar(
    calendar_path: Path,
    target_date: date,
    lookahead_days: int = 3,
) -> list[CalendarEvent]:
    """Load events from manual economic calendar for the next N days."""
    try:
        with open(calendar_path) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[ADVISOR] Calendar load failed: {e}")
        return []

    events: list[CalendarEvent] = []
    end_date = target_date + timedelta(days=lookahead_days)
    for entry in raw:
        try:
            event_date = date.fromisoformat(entry["date"])
            if target_date <= event_date <= end_date:
                events.append(CalendarEvent(
                    date=event_date,
                    event=entry["event"],
                    impact=entry.get("impact", "MEDIUM"),
                ))
        except (KeyError, ValueError):
            continue
    return events


# ── Main Context Fetcher ─────────────────────────────────────────

async def fetch_external_context(
    target_date: date,
    calendar_path: Path | None = None,
    skip_external: bool = False,
    nifty_prev_close: float = 0,
) -> ExternalContext:
    """Fetch all external context for the target date.

    Args:
        target_date: The date to analyze (usually tomorrow).
        calendar_path: Path to economic_calendar.json.
        skip_external: If True, skip all network requests (offline mode).
        nifty_prev_close: Previous NIFTY close (for GIFT Nifty change calc).
    """
    available: list[str] = []
    headlines: list[str] = []
    global_headlines: list[str] = []
    fii_dii: FiiDiiData | None = None
    global_markets: GlobalMarketData | None = None
    events: list[CalendarEvent] = []

    if not skip_external:
        # 1. India news headlines
        headlines = await _fetch_news_headlines()
        if headlines:
            available.append("google_news")
            logger.info(f"[ADVISOR] Fetched {len(headlines)} India news headlines")

        # 2. Global market headlines
        global_headlines = await _fetch_global_headlines()
        if global_headlines:
            available.append("global_headlines")
            logger.info(f"[ADVISOR] Fetched {len(global_headlines)} global headlines")

        # 3. FII/DII flows
        fii_dii = await _fetch_fii_dii()
        if fii_dii:
            available.append("nse_fii_dii")
            logger.info(f"[ADVISOR] FII={fii_dii.fii_net:+,.0f} DII={fii_dii.dii_net:+,.0f}")

        # 4. US markets, VIX, crude, DXY (Yahoo Finance)
        global_markets = await _fetch_global_markets()
        fetched_global = []
        if global_markets.sp500_close > 0:
            fetched_global.append(f"S&P={global_markets.sp500_change_pct:+.1f}%")
        if global_markets.us_vix > 0:
            fetched_global.append(f"VIX={global_markets.us_vix:.1f}")
        if global_markets.crude_oil_close > 0:
            fetched_global.append(f"Crude={global_markets.crude_oil_change_pct:+.1f}%")

        if fetched_global:
            available.append("yahoo_finance")
            logger.info(f"[ADVISOR] Global markets: {', '.join(fetched_global)} sentiment={global_markets.global_sentiment}")

        # 5. GIFT Nifty pre-market
        gift_price, gift_chg = await _fetch_gift_nifty(nifty_prev_close)
        if gift_price > 0 and global_markets:
            global_markets.gift_nifty = gift_price
            global_markets.gift_nifty_change_pct = gift_chg
            available.append("gift_nifty")
            logger.info(f"[ADVISOR] GIFT Nifty={gift_price:.0f} ({gift_chg:+.2f}%)")

    # 6. Economic calendar (local file, always available)
    if calendar_path and calendar_path.exists():
        events = _load_economic_calendar(calendar_path, target_date)
        if events:
            available.append("economic_calendar")
            logger.info(f"[ADVISOR] {len(events)} upcoming events loaded")

    return ExternalContext(
        is_trading_day=target_date.weekday() < 5,
        news_headlines=headlines,
        global_headlines=global_headlines,
        fii_dii=fii_dii,
        global_markets=global_markets,
        events=events,
        available_sources=available,
    )
