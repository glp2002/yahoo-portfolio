"""Fetch Yahoo Finance portfolio data via the authenticated Playwright session,
then enrich each holding with public market data via yfinance.

Portfolio pages are fetched in parallel via asyncio + Playwright async API
(bounded by a semaphore so we don't open dozens of tabs at once). Market
data enrichment uses a thread pool for yfinance (a sync library).
"""
import asyncio
import re
import time as _time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional

from playwright.async_api import async_playwright, BrowserContext, Page

from auth import SESSION_DIR, PORTFOLIOS_URL, is_auth_url

NAV_TIMEOUT_MS = 30000
SELECTOR_TIMEOUT_MS = 15000
MAX_CONCURRENT_FETCHES = 6


def _log(msg: str) -> None:
    print(f"[YP {_time.strftime('%H:%M:%S')}] {msg}", flush=True)


class NotAuthenticatedError(Exception):
    pass


# ----------------------------------------------------------------------
# Public entry point (sync facade for the GUI)
# ----------------------------------------------------------------------
def fetch_all_portfolios() -> List[Dict[str, Any]]:
    _log("fetch_all_portfolios: start")
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    # Windows: Playwright async needs the ProactorEventLoopPolicy, which is
    # the default on Python 3.8+. asyncio.run() creates its own loop here.
    results = asyncio.run(_fetch_all_async())

    _enrich_with_market_data(results)
    _log("fetch_all_portfolios: done")
    return results


async def _fetch_all_async() -> List[Dict[str, Any]]:
    async with async_playwright() as p:
        _log("launching persistent context (headless=True)")
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=True,
        )
        _log("context launched")
        try:
            portfolios_meta = await _list_portfolios_async(context)
            _log(f"found {len(portfolios_meta)} portfolio(s): "
                 f"{[pf['name'] for pf in portfolios_meta]}")
            if not portfolios_meta:
                return []

            sem = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
            _log(f"fetching {len(portfolios_meta)} portfolio(s) "
                 f"with concurrency={MAX_CONCURRENT_FETCHES}")

            async def _bounded(pf):
                async with sem:
                    _log(f"  -> start '{pf['name']}'  ({pf['url']})")
                    t0 = _time.monotonic()
                    data = await _fetch_portfolio_data_async(context, pf["url"])
                    dt = _time.monotonic() - t0
                    _log(f"  <- done  '{pf['name']}' in {dt:.1f}s "
                         f"({len(data.get('holdings', []))} holdings)")
                    return data

            datas = await asyncio.gather(*[_bounded(pf) for pf in portfolios_meta])

            results: List[Dict[str, Any]] = []
            for pf, data in zip(portfolios_meta, datas):
                final_name = (data.get("name") or "") or pf["name"]
                results.append({
                    "name": final_name,
                    "holdings": data.get("holdings", []),
                })
            return results
        finally:
            _log("closing context")
            await context.close()


# ----------------------------------------------------------------------
# List all portfolios (single page operation, must be done first)
# ----------------------------------------------------------------------
async def _list_portfolios_async(context: BrowserContext) -> List[Dict[str, str]]:
    page = await context.new_page()
    try:
        _log(f"navigating to {PORTFOLIOS_URL}")
        await page.goto(PORTFOLIOS_URL, wait_until="domcontentloaded",
                        timeout=NAV_TIMEOUT_MS)
        _log(f"landed at: {page.url}")
        _log(f"page title: {(await page.title())!r}")
        if is_auth_url(page.url):
            raise NotAuthenticatedError(
                "Yahoo redirected to login/consent -- click 'Log in to Yahoo' first."
            )
        try:
            await page.wait_for_selector('a[href*="/portfolio/p_"]',
                                          timeout=SELECTOR_TIMEOUT_MS,
                                          state="attached")
        except Exception as e:
            _log(f"portfolio selector wait failed: {type(e).__name__}: {e}")
            html = (await page.content()) or ""
            dump = SESSION_DIR.parent / "last_page_dump.html"
            try:
                dump.write_text(html, encoding="utf-8")
                _log(f"wrote page HTML to {dump}")
            except Exception:
                pass
            if "sign in" in html.lower() and "portfolio" in html.lower():
                raise NotAuthenticatedError(
                    "Yahoo is showing a sign-in prompt -- click 'Log in to Yahoo'."
                )
            return []

        anchors = await page.query_selector_all('a[href*="/portfolio/"]')
        seen = set()
        portfolios: List[Dict[str, str]] = []
        for a in anchors:
            href = (await a.get_attribute("href")) or ""
            m = re.search(r"(/portfolio/p_\d+)", href)
            if not m:
                continue
            base = m.group(1)
            if base in seen:
                continue
            seen.add(base)
            try:
                name = ((await a.text_content()) or "").strip()
            except Exception:
                name = ""
            if not name:
                name = base
            url = "https://finance.yahoo.com" + base + "/view"
            portfolios.append({"name": name, "url": url})
        return portfolios
    finally:
        await page.close()


# ----------------------------------------------------------------------
# Per-portfolio fetch (one task per portfolio, runs in parallel)
# ----------------------------------------------------------------------
def _classify_header(text: str) -> Optional[str]:
    t = (text or "").strip().lower()
    if not t:
        return None
    has_change_word = "chg" in t or "change" in t
    has_pct = ("%" in t) or ("pct" in t) or ("percent" in t)
    if "symbol" in t:
        return "symbol"
    if has_change_word and has_pct:
        return "change_pct"
    if has_change_word:
        return "change"
    if ("last price" in t or "market price" in t
            or t == "price" or t == "last"):
        return "last_price"
    return None


_GENERIC_NAMES = {
    "yahoo finance",
    "yahoo",
    "portfolio",
    "portfolios",
    "my portfolios",
    "stock portfolio management & tracker",
}


async def _extract_portfolio_name_async(page: Page) -> str:
    def _ok(s: str) -> bool:
        return bool(s) and s.lower().strip(" |-") not in _GENERIC_NAMES

    h1_text = ""
    try:
        h1 = await page.query_selector("h1")
        if h1:
            h1_text = ((await h1.text_content()) or "").strip()
    except Exception:
        pass

    title = ""
    try:
        title = ((await page.title()) or "").strip()
    except Exception:
        pass

    if _ok(h1_text):
        return h1_text
    for sep in (" | ", " - "):
        if sep in title:
            head = title.split(sep, 1)[0].strip()
            if _ok(head):
                return head
    if _ok(title):
        return title
    return ""


async def _fetch_portfolio_data_async(context: BrowserContext, portfolio_url: str) -> Dict[str, Any]:
    page = await context.new_page()
    try:
        await page.goto(portfolio_url, wait_until="domcontentloaded",
                        timeout=NAV_TIMEOUT_MS)

        try:
            await page.wait_for_selector("table tbody tr",
                                          timeout=SELECTOR_TIMEOUT_MS,
                                          state="attached")
        except Exception as e:
            _log(f"  table selector wait failed for {portfolio_url}: {type(e).__name__}: {e}")
            return {"name": await _extract_portfolio_name_async(page), "holdings": []}

        name = await _extract_portfolio_name_async(page)
        holdings = await _parse_holdings_tables_async(page)
        return {"name": name, "holdings": holdings}
    finally:
        await page.close()


async def _parse_holdings_tables_async(page: Page) -> List[Dict[str, str]]:
    tables = await page.query_selector_all("table")
    for table_idx, table in enumerate(tables):
        header_cells = await table.query_selector_all("thead th")
        if not header_cells:
            header_cells = await table.query_selector_all(
                "tr:first-child th, tr:first-child td"
            )
        if not header_cells:
            continue

        header_texts: List[str] = []
        for h in header_cells:
            header_texts.append(((await h.inner_text()) or "").strip())

        col_map: Dict[str, int] = {}
        for idx, h_text in enumerate(header_texts):
            key = _classify_header(h_text)
            if key and key not in col_map:
                col_map[key] = idx

        if "symbol" not in col_map or "last_price" not in col_map:
            continue

        rows_out: List[Dict[str, str]] = []
        body_rows = await table.query_selector_all("tbody tr")
        if not body_rows:
            all_rows = await table.query_selector_all("tr")
            body_rows = all_rows[1:]
        max_idx = max(col_map.values())
        for row in body_rows:
            cells = await row.query_selector_all("td")
            if not cells or len(cells) <= max_idx:
                continue

            async def get(key: str) -> str:
                i = col_map.get(key)
                if i is None:
                    return ""
                return ((await cells[i].inner_text()) or "").strip()

            symbol = await get("symbol")
            if not symbol:
                continue
            rows_out.append({
                "symbol":     symbol,
                "last_price": await get("last_price"),
                "change":     await get("change"),
                "change_pct": await get("change_pct"),
            })
        if rows_out:
            return rows_out
    return []


# ----------------------------------------------------------------------
# yfinance enrichment (sync; threaded for parallelism)
# ----------------------------------------------------------------------
def _fmt_num(v) -> str:
    if v is None:
        return ""
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_range(low, high) -> str:
    if low is None or high is None:
        return ""
    try:
        return f"{float(low):.2f} - {float(high):.2f}"
    except (TypeError, ValueError):
        return ""


def _fetch_one_symbol_info(symbol: str) -> Dict[str, Any]:
    try:
        import yfinance as yf
        return yf.Ticker(symbol).info or {}
    except Exception as e:
        _log(f"  yfinance error for {symbol}: {type(e).__name__}: {e}")
        return {}


def _enrich_with_market_data(portfolios: List[Dict[str, Any]]) -> None:
    symbols: List[str] = []
    seen = set()
    for pf in portfolios:
        for h in pf.get("holdings", []):
            s = (h.get("symbol") or "").strip()
            if s and s not in seen:
                seen.add(s)
                symbols.append(s)

    for pf in portfolios:
        for h in pf.get("holdings", []):
            h.setdefault("pe_ttm", "")
            h.setdefault("forward_pe", "")
            h.setdefault("range_52w", "")
            h.setdefault("range_low", None)
            h.setdefault("range_high", None)

    if not symbols:
        return

    try:
        import yfinance  # noqa: F401
    except ImportError:
        _log("yfinance is NOT installed -- run: pip install yfinance")
        _log("skipping market-data enrichment; P/E and 52-wk Range will be empty")
        return

    _log(f"yfinance: fetching market data for {len(symbols)} unique symbol(s)")
    data: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for sym, info in zip(symbols, ex.map(_fetch_one_symbol_info, symbols)):
            data[sym] = info
    _log("yfinance: done")

    for sym in symbols:
        info = data.get(sym) or {}
        if info:
            _log(f"yfinance fields for {sym} ({len(info)} total): {sorted(info.keys())}")
            break

    for pf in portfolios:
        for h in pf.get("holdings", []):
            info = data.get(h.get("symbol", ""), {})
            low  = info.get("fiftyTwoWeekLow")
            high = info.get("fiftyTwoWeekHigh")
            h["pe_ttm"]     = _fmt_num(info.get("trailingPE"))
            h["forward_pe"] = _fmt_num(info.get("forwardPE"))
            h["range_52w"]  = _fmt_range(low, high)
            try:
                h["range_low"]  = float(low)  if low  is not None else None
                h["range_high"] = float(high) if high is not None else None
            except (TypeError, ValueError):
                h["range_low"] = h["range_high"] = None
