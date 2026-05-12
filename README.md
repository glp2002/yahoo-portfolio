<p align="center">
  <img src="icon.png" alt="Yahoo Portfolio" width="128" height="128">
</p>

<h1 align="center">Yahoo Portfolio</h1>

<p align="center">
  A small Windows desktop app that pulls every portfolio off your Yahoo Finance
  account, enriches each holding with fresh market data, and shows it all in a
  single sortable Tk window with inline 52-week range bars.
</p>

---

## Features

- Fetches **all** portfolios from `finance.yahoo.com/portfolios` in parallel
  via a persistent Playwright session — no API keys, no scraping tokens.
- Enriches every symbol with `yfinance` (last price, change, P/E, forward P/E,
  52-wk low/high).
- Per-portfolio sections with a custom grid table: click any column header to
  sort, click again to reverse.
- Inline 52-week range bar per row that redraws at the column's actual pixel
  width as you resize the window.
- Persistent login: log in once via a visible browser, session cookies live
  under `.yahoo_session/` and are reused on subsequent runs.

## Requirements

- Windows 10/11
- Python 3.10+ (developed on 3.14)
- A Yahoo Finance account with one or more portfolios

## Install

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Run

```bash
python main.py
```

First run: click **Log in to Yahoo**, complete login in the browser window that
opens, then close that window. Click **Refresh** to load your portfolios. The
session is cached, so subsequent runs skip the login step.

## Desktop shortcut (optional)

Create a `Yahoo Portfolio.lnk` on your desktop pointing at `pythonw.exe` with
`main.py` as the argument and the project folder as "Start in". Set the icon to
`icon.ico` for a clean no-console launcher.

## Project layout

| File             | What it does                                                       |
|------------------|--------------------------------------------------------------------|
| `main.py`        | Entry point — boots the Tk GUI.                                    |
| `gui.py`         | Tk window, table rendering, sort, 52-wk range bar.                 |
| `portfolio.py`   | Playwright + yfinance: fetches portfolios and market data.         |
| `auth.py`        | Interactive Yahoo login via a visible Chromium window.             |
| `requirements.txt` | Python dependencies (`playwright`, `yfinance`).                  |
| `icon.ico`       | App icon used by the Windows shortcut.                             |
| `icon.png`       | PNG version of the icon (used in this README).                     |

## Notes

- `.yahoo_session/` and `last_page_dump.html` are gitignored — the first holds
  login cookies, the second is a local debug dump produced when scraping fails.
- This is a personal tool against Yahoo's public web UI. If Yahoo changes the
  page structure, scraping in `portfolio.py` may need to be updated.
