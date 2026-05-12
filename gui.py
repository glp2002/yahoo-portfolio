"""Tkinter GUI: one section per portfolio, manual refresh, threaded fetch.

Custom grid-based table (replaces ttk.Treeview) so each row can host a
Canvas for the 52-wk range bar. Features:
  - Click a column header to sort; click again to reverse.
  - Table stretches to fill the window; the bar redraws at the column's
    actual pixel width whenever the window is resized.
  - Double-click any row to open ChatGPT in the default browser asking
    why that stock is moving today (prompt also copied to the clipboard
    as a fallback).
"""
import datetime
import re
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk, messagebox
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from auth import interactive_login
from portfolio import fetch_all_portfolios, NotAuthenticatedError


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _change_sign(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    cleaned = s.replace("(", "").replace(")", "").strip()
    if cleaned.startswith("+"):
        return "pos"
    if cleaned.startswith("-") or cleaned.startswith("−"):
        return "neg"
    try:
        n = float(cleaned.replace("%", "").replace(",", ""))
        if n > 0:
            return "pos"
        if n < 0:
            return "neg"
    except ValueError:
        pass
    return ""


_NUM_RE = re.compile(r'[-+]?\d+(?:\.\d+)?')


def _sort_key_str(s: str):
    s = (s or "").strip()
    if not s:
        return (2, "")
    m = _NUM_RE.search(s)
    if m:
        try:
            return (0, float(m.group()))
        except ValueError:
            pass
    return (1, s.lower())


def _parse_price(s) -> Optional[float]:
    if s is None:
        return None
    s = str(s)
    if not s:
        return None
    m = _NUM_RE.search(s.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


# ----------------------------------------------------------------------
# columns: (key, label, char_width_min, anchor, kind, weight)
# ----------------------------------------------------------------------
_COLS: List[Tuple[str, str, int, str, str, int]] = [
    ("symbol",     "Symbol",       8,  "w",      "text", 1),
    ("last_price", "Last Price",   10, "e",      "text", 1),
    ("change",     "Change",       9,  "e",      "text", 1),
    ("change_pct", "Change %",     9,  "e",      "text", 1),
    ("pe_ttm",     "P/E (TTM)",    9,  "e",      "text", 1),
    ("forward_pe", "Forward P/E",  10, "e",      "text", 1),
    ("range_52w",  "52-wk Range",  20, "center", "bar",  3),
]

_BAR_MIN_WIDTH_PX = 220
_BAR_HEIGHT_PX = 22


def _draw_range_bar(canvas: tk.Canvas,
                    low: Optional[float],
                    high: Optional[float],
                    current: Optional[float]) -> None:
    canvas.delete("all")
    W = max(int(canvas.winfo_width()), _BAR_MIN_WIDTH_PX)
    H = _BAR_HEIGHT_PX
    pad = 56
    y_top = 9
    y_bot = 15

    if low is None or high is None or low >= high:
        canvas.create_text(W / 2, H / 2, text="(no range data)",
                           fill="#999", font=("Segoe UI", 8))
        return

    canvas.create_rectangle(pad, y_top, W - pad, y_bot,
                            fill="#e6e6e6", outline="#bbb")

    if current is not None:
        frac = (current - low) / (high - low)
        frac = max(0.0, min(1.0, frac))
        x = pad + frac * (W - 2 * pad)
        canvas.create_rectangle(pad, y_top, x, y_bot,
                                fill="#4a90e2", outline="")
        canvas.create_oval(x - 4, y_top - 3, x + 4, y_bot + 3,
                           fill="#1a5a9c", outline="white", width=1)

    canvas.create_text(pad - 4, H / 2, text=f"{low:.2f}",
                       anchor="e", fill="#333", font=("Segoe UI", 8))
    canvas.create_text(W - pad + 4, H / 2, text=f"{high:.2f}",
                       anchor="w", fill="#333", font=("Segoe UI", 8))


# ----------------------------------------------------------------------
# App
# ----------------------------------------------------------------------
class PortfolioApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Yahoo Portfolio")
        root.geometry("1180x740")

        toolbar = tk.Frame(root)
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)
        self.login_btn = tk.Button(toolbar, text="Log in to Yahoo", width=16,
                                   command=self.on_login)
        self.login_btn.pack(side=tk.LEFT)
        self.refresh_btn = tk.Button(toolbar, text="Refresh", width=10,
                                     command=self.on_refresh)
        self.refresh_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.status = tk.Label(
            toolbar,
            text="Click 'Log in to Yahoo' first, then Refresh. "
                 "Double-click any row to ask ChatGPT about that stock.",
            anchor="w",
        )
        self.status.pack(side=tk.LEFT, padx=10)

        outer = tk.Frame(root)
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=self.canvas.yview)
        self.content = tk.Frame(self.canvas)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._content_window = self.canvas.create_window(
            (0, 0), window=self.content, anchor="nw"
        )

        self.content.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfig(self._content_window, width=e.width),
        )
        self.canvas.bind_all(
            "<MouseWheel>",
            lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )

        self._busy = False

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.refresh_btn.config(state=state)
        self.login_btn.config(state=state)

    # --- Double-click handler: ask ChatGPT about a stock ---
    def _open_chatgpt_for_symbol(self, symbol: str) -> None:
        sym = (symbol or "").strip()
        if not sym:
            return
        prompt = (f"Why is {sym} stock moving today? "
                  f"What recent news or events might explain the price action?")
        # Copy to clipboard as a fallback.
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(prompt)
        except Exception:
            pass
        # ChatGPT honors ?q= as a pre-filled, auto-submitted prompt.
        url = f"https://chatgpt.com/?q={quote(prompt)}"
        webbrowser.open(url)
        self.status.config(
            text=f"Asked ChatGPT about {sym}. Prompt also copied to clipboard."
        )

    # --- Login ---
    def on_login(self):
        if self._busy:
            return
        self._set_busy(True)
        self.status.config(
            text="Browser opened. Log in to Yahoo, then close the browser window when done."
        )

        def work():
            try:
                ok = interactive_login()
            except Exception as e:
                self.root.after(0, lambda exc=e: self._on_error(exc, action="Login"))
                return
            self.root.after(0, lambda: self._on_login_done(ok))

        threading.Thread(target=work, daemon=True).start()

    def _on_login_done(self, ok: bool):
        if ok:
            self.status.config(text="Browser closed. Click Refresh to load portfolios.")
        else:
            self.status.config(text="Login timed out. Click 'Log in to Yahoo' to retry.")
        self._set_busy(False)

    # --- Refresh ---
    def on_refresh(self):
        if self._busy:
            return
        self._set_busy(True)
        self.status.config(text="Loading portfolios...")

        def work():
            try:
                data = fetch_all_portfolios()
            except NotAuthenticatedError as e:
                self.root.after(0, lambda exc=e: self._on_not_authed(exc))
                return
            except Exception as e:
                self.root.after(0, lambda exc=e: self._on_error(exc, action="Refresh"))
                return
            self.root.after(0, lambda: self._on_data(data))

        threading.Thread(target=work, daemon=True).start()

    def _on_data(self, portfolios: List[Dict[str, Any]]):
        self._render(portfolios)
        total = sum(len(pf.get("holdings", [])) for pf in portfolios)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.status.config(
            text=f"Loaded {len(portfolios)} portfolio(s), {total} holding(s) "
                 f"· Fetched: {ts}  ·  Double-click a row to ask ChatGPT."
        )
        self._set_busy(False)

    def _on_not_authed(self, exc: NotAuthenticatedError):
        self.status.config(text="Not logged in -- click 'Log in to Yahoo'.")
        self._set_busy(False)
        messagebox.showinfo("Not logged in", str(exc))

    def _on_error(self, exc: Exception, action: str = "Operation"):
        self.status.config(text=f"{action} failed.")
        self._set_busy(False)
        messagebox.showerror(f"{action} failed", f"{type(exc).__name__}: {exc}")

    # --- Rendering ---
    def _render(self, portfolios: List[Dict[str, Any]]):
        for child in self.content.winfo_children():
            child.destroy()
        if not portfolios:
            tk.Label(self.content,
                     text="No portfolios found on your Yahoo Finance account.",
                     pady=20).pack()
            return
        for pf in portfolios:
            self._render_portfolio(pf)

    def _render_portfolio(self, pf: Dict[str, Any]):
        name = pf.get("name") or "(unnamed portfolio)"
        holdings: List[Dict[str, Any]] = list(pf.get("holdings", []))

        section = tk.Frame(self.content, pady=6)
        section.pack(fill=tk.X, expand=True, padx=4, pady=4)

        tk.Label(section, text=f"{name}  -  {len(holdings)} holding(s)",
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(fill=tk.X, pady=(0, 4))

        if not holdings:
            tk.Label(section, text="(no holdings)", fg="#777", anchor="w").pack(fill=tk.X)
            return

        table = tk.Frame(section)
        table.pack(fill=tk.X, expand=True)

        for c_idx, (_, _, min_w, _, kind, weight) in enumerate(_COLS):
            minsize = _BAR_MIN_WIDTH_PX if kind == "bar" else max(min_w * 8, 70)
            table.grid_columnconfigure(c_idx, weight=weight, minsize=minsize)

        state = {"col": None, "desc": False}

        def render_table():
            for w in table.winfo_children():
                w.destroy()

            # Header row -- clickable sort
            for c_idx, (key, label, min_w, anchor, kind, _w) in enumerate(_COLS):
                ind = ""
                if state["col"] == key:
                    ind = "  v" if state["desc"] else "  ^"
                hdr = tk.Label(
                    table, text=label + ind,
                    anchor=anchor,
                    font=("Segoe UI", 9, "bold"),
                    fg="#1a5a9c", bg="#f0f0f0",
                    cursor="hand2",
                    padx=4, pady=3,
                    relief="ridge", borderwidth=1,
                )
                hdr.grid(row=0, column=c_idx, sticky="ew")
                hdr.bind("<Button-1>", lambda e, k=key: sort_by(k))

            # Data rows
            for r_idx, h in enumerate(holdings, start=1):
                bg = "#ffffff" if r_idx % 2 else "#fafafa"
                sym = (h.get("symbol") or "").strip()
                dbl_handler = (lambda e, s=sym: self._open_chatgpt_for_symbol(s))
                for c_idx, (key, label, min_w, anchor, kind, _w) in enumerate(_COLS):
                    if kind == "bar":
                        cv = tk.Canvas(
                            table,
                            height=_BAR_HEIGHT_PX,
                            highlightthickness=0, bg=bg,
                            cursor="hand2",
                        )
                        low = h.get("range_low")
                        high = h.get("range_high")
                        cur = _parse_price(h.get("last_price", ""))
                        def make_redraw(cv_ref, lo, hi, p):
                            def _redraw(event=None):
                                _draw_range_bar(cv_ref, lo, hi, p)
                            return _redraw
                        cv.bind("<Configure>", make_redraw(cv, low, high, cur))
                        cv.bind("<Double-Button-1>", dbl_handler)
                        cv.grid(row=r_idx, column=c_idx, sticky="ew", padx=0)
                    else:
                        val = str(h.get(key, ""))
                        fg = "#000"
                        if key in ("change", "change_pct"):
                            sign = _change_sign(val)
                            if sign == "pos":
                                fg = "#0a7a0a"
                            elif sign == "neg":
                                fg = "#b40000"
                        lbl = tk.Label(table, text=val, anchor=anchor,
                                       font=("Segoe UI", 9), fg=fg, bg=bg,
                                       padx=4, pady=2, cursor="hand2")
                        lbl.bind("<Double-Button-1>", dbl_handler)
                        lbl.grid(row=r_idx, column=c_idx, sticky="ew")

        def sort_by(col: str):
            if state["col"] == col:
                state["desc"] = not state["desc"]
            else:
                state["col"] = col
                state["desc"] = False

            def key_fn(h: Dict[str, Any]):
                if col == "range_52w":
                    v = h.get("range_low")
                    if v is None:
                        return (2, "")
                    return (0, float(v))
                v = h.get(col)
                if v is None:
                    return (2, "")
                return _sort_key_str(str(v))

            holdings.sort(key=key_fn, reverse=state["desc"])
            render_table()

        render_table()


def main():
    root = tk.Tk()
    PortfolioApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
