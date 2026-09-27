import os
import math
import time
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

# ============================================================
# DELTA EXCHANGE INDIA - SUPERTREND GRID BACKTEST
# Historical candles come directly from Delta's public candle API.
# No live orders are sent by this file.
# ============================================================

BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange").rstrip("/")
SYMBOL = os.getenv("DELTA_SYMBOL", "BTCUSD")
PRODUCT_ID = int(os.getenv("DELTA_PRODUCT_ID", "27"))
CONTRACT_BTC = 0.001

ATR_PERIOD = 10
MULTIPLIER = 3.0
GRID_STEP = 300
GRID_CHUNK_CONTRACTS = 1
GRID_INITIAL_CONTRACTS = 10
GRID_UPPER_LEVELS = 10
TIMEFRAME = "1h"
CANDLE_SECONDS = 3600

st.set_page_config(page_title="Delta SuperTrend Grid Backtest", page_icon="📊", layout="wide")


def fmt(x, n=2):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x:,.{n}f}"


def fetch_candles(symbol: str, resolution: str, days: int):
    seconds = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400}[resolution]
    end = int(time.time())
    start = end - days * 86400
    url = f"{BASE_URL}/v2/history/candles"
    r = requests.get(url, params={"symbol": symbol, "resolution": resolution, "start": start, "end": end}, timeout=30)
    r.raise_for_status()
    data = r.json()
    rows = data.get("result", data)
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Delta candle API returned no candles: {data}")
    out = []
    for c in rows:
        if not isinstance(c, dict):
            continue
        t = c.get("time")
        if t is None:
            continue
        out.append({
            "time": int(t),
            "open": float(c.get("open", 0)),
            "high": float(c.get("high", 0)),
            "low": float(c.get("low", 0)),
            "close": float(c.get("close", 0)),
            "volume": float(c.get("volume", 0)),
        })
    df = pd.DataFrame(out).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    current_bucket = (int(time.time()) // seconds) * seconds
    return df[df.time < current_bucket].reset_index(drop=True)


def calculate_supertrend(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    df["TR"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["ATR"] = float("nan")
    if len(df) >= ATR_PERIOD:
        df.loc[ATR_PERIOD - 1, "ATR"] = df["TR"].iloc[:ATR_PERIOD].mean()
        for i in range(ATR_PERIOD, len(df)):
            df.loc[i, "ATR"] = (df.loc[i - 1, "ATR"] * (ATR_PERIOD - 1) + df.loc[i, "TR"]) / ATR_PERIOD

    df["HL2"] = (df.high + df.low) / 2.0
    for col in ["BASIC_UPPER", "BASIC_LOWER", "FINAL_UPPER", "FINAL_LOWER", "SUPERTREND"]:
        df[col] = float("nan")
    df["ST_DIRECTION"] = 0
    df["SIGNAL"] = ""

    prev_final_upper = None
    prev_final_lower = None
    prev_supertrend = None
    prev_direction = 1

    for i in range(len(df)):
        atr = df.loc[i, "ATR"]
        if pd.isna(atr):
            continue
        hl2 = float(df.loc[i, "HL2"])
        close = float(df.loc[i, "close"])
        basic_upper = hl2 + MULTIPLIER * float(atr)
        basic_lower = hl2 - MULTIPLIER * float(atr)
        df.loc[i, "BASIC_UPPER"] = basic_upper
        df.loc[i, "BASIC_LOWER"] = basic_lower

        if i == ATR_PERIOD - 1 or prev_final_upper is None:
            final_upper, final_lower = basic_upper, basic_lower
        else:
            previous_close = float(df.loc[i - 1, "close"])
            final_upper = basic_upper if (basic_upper < prev_final_upper or previous_close > prev_final_upper) else prev_final_upper
            final_lower = basic_lower if (basic_lower > prev_final_lower or previous_close < prev_final_lower) else prev_final_lower
        df.loc[i, "FINAL_UPPER"] = final_upper
        df.loc[i, "FINAL_LOWER"] = final_lower

        if i == ATR_PERIOD - 1 or prev_supertrend is None:
            direction, supertrend = 1, final_upper
        else:
            if prev_supertrend == prev_final_upper:
                if close <= final_upper:
                    direction, supertrend = 1, final_upper
                else:
                    direction, supertrend = -1, final_lower
            else:
                if close >= final_lower:
                    direction, supertrend = -1, final_lower
                else:
                    direction, supertrend = 1, final_upper
        df.loc[i, "ST_DIRECTION"] = direction
        df.loc[i, "SUPERTREND"] = supertrend
        if i > 0 and prev_direction != direction:
            df.loc[i, "SIGNAL"] = "BUY" if direction == -1 else "SELL"
        prev_final_upper, prev_final_lower = final_upper, final_lower
        prev_supertrend, prev_direction = supertrend, direction
    return df


def path_points(row, mode):
    o, h, l, c = float(row.open), float(row.high), float(row.low), float(row.close)
    if mode == "OHLC":
        return [(o, "OPEN"), (h, "HIGH"), (l, "LOW"), (c, "CLOSE")]
    if mode == "OLHC":
        return [(o, "OPEN"), (l, "LOW"), (h, "HIGH"), (c, "CLOSE")]
    # Heuristic: candle normally travels first toward the closer extreme implied by its close.
    return [(o, "OPEN"), (l, "LOW"), (h, "HIGH"), (c, "CLOSE")] if c >= o else [(o, "OPEN"), (h, "HIGH"), (l, "LOW"), (c, "CLOSE")]


def crossed(prev_price, price, level, direction):
    if direction == "UP":
        return prev_price < level <= price
    return prev_price > level >= price


def run_backtest(df, starting_balance, leverage, fee_rate, entry_offset, path_mode, max_signals=None):
    cash = float(starting_balance)
    peak_equity = cash
    max_drawdown = 0.0
    position = 0
    avg_entry = 0.0
    cycle_id = 0
    active = None
    order_rows = []
    cycle_rows = []
    equity_rows = []
    signal_count = 0
    blocked_count = 0
    realized_total = 0.0
    fees_total = 0.0

    def required_margin(price, contracts):
        notional = abs(contracts) * CONTRACT_BTC * price
        return notional / max(leverage, 1e-9)

    def close_position(price, reason, t, candle_index):
        nonlocal position, avg_entry, cash, realized_total, fees_total
        if position == 0:
            return 0.0
        qty_btc = abs(position) * CONTRACT_BTC
        gross = (price - avg_entry) * qty_btc if position > 0 else (avg_entry - price) * qty_btc
        fee = abs(price * qty_btc) * fee_rate
        net = gross - fee
        cash += net
        realized_total += net
        fees_total += fee
        order_rows.append({"Time": t, "Candle": candle_index, "Cycle": cycle_id, "Type": "REVERSAL_CLOSE", "Side": "SELL" if position > 0 else "BUY", "Price": price, "Contracts": abs(position), "BTC": qty_btc, "Gross P/L": gross, "Fee": fee, "Net P/L": net, "Reason": reason})
        position = 0
        avg_entry = 0.0
        return net

    def fill_order(side, price, contracts, label, t, candle_index):
        nonlocal position, avg_entry, cash, realized_total, fees_total
        qty = int(contracts)
        qty_btc = qty * CONTRACT_BTC
        fee = abs(price * qty_btc) * fee_rate
        margin = required_margin(price, qty)
        if margin > cash and side == ("BUY" if position >= 0 else "SELL"):
            return False, "INSUFFICIENT_MARGIN"
        old_pos = position
        new_pos = position + qty if side == "BUY" else position - qty
        realized = 0.0
        # Same-direction add: weighted average.
        if old_pos == 0 or (old_pos > 0 and side == "BUY") or (old_pos < 0 and side == "SELL"):
            old_abs = abs(old_pos)
            new_abs = abs(new_pos)
            avg_entry = price if old_abs == 0 else ((avg_entry * old_abs) + (price * qty)) / new_abs
            position = new_pos
            cash -= fee
            fees_total += fee
            order_rows.append({"Time": t, "Candle": candle_index, "Cycle": cycle_id, "Type": label, "Side": side, "Price": price, "Contracts": qty, "BTC": qty_btc, "Gross P/L": 0.0, "Fee": fee, "Net P/L": -fee, "Reason": "FILLED"})
            return True, "FILLED"
        # Opposite direction: reduce/close existing inventory.
        close_qty = min(abs(old_pos), qty)
        gross = (price - avg_entry) * (close_qty * CONTRACT_BTC) if old_pos > 0 else (avg_entry - price) * (close_qty * CONTRACT_BTC)
        realized = gross - fee
        cash += realized
        realized_total += realized
        fees_total += fee
        remaining = abs(old_pos) - close_qty
        position = (1 if old_pos > 0 else -1) * remaining
        if position == 0:
            avg_entry = 0.0
        order_rows.append({"Time": t, "Candle": candle_index, "Cycle": cycle_id, "Type": label, "Side": side, "Price": price, "Contracts": qty, "BTC": qty_btc, "Gross P/L": gross, "Fee": fee, "Net P/L": realized, "Reason": "FILLED"})
        return True, "FILLED"

    for i, row in df.iterrows():
        t = datetime.fromtimestamp(int(row.time), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        close = float(row.close)
        equity = cash + ((close - avg_entry) * position * CONTRACT_BTC if position else 0.0)
        equity_rows.append({"Time": t, "Equity": equity, "Cash": cash, "Position Contracts": position, "Price": close})
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, peak_equity - equity)

        sig = str(row.SIGNAL)
        if sig in ("BUY", "SELL"):
            signal_count += 1
            if max_signals and signal_count > max_signals:
                break
            # Reversal is evaluated at the completed candle close. Old orders are gone before new cycle.
            if active is not None:
                close_position(close, "SUPERTREND_REVERSAL", t, i)
                cycle_rows.append({"Cycle": cycle_id, "Direction": active["direction"], "Signal Time": active["signal_time"], "Signal Price": active["base"], "Close Time": t, "Close Price": close, "Reason": "REVERSAL", "Net P/L": cycle_pnl(active, order_rows)})
                active = None

            direction = sig
            base = close + entry_offset if direction == "BUY" else close - entry_offset
            # Check margin for initial inventory.
            if required_margin(base, GRID_INITIAL_CONTRACTS) > cash:
                blocked_count += 1
                order_rows.append({"Time": t, "Candle": i, "Cycle": cycle_id + 1, "Type": "INITIAL_ENTRY", "Side": direction, "Price": base, "Contracts": GRID_INITIAL_CONTRACTS, "BTC": GRID_INITIAL_CONTRACTS * CONTRACT_BTC, "Gross P/L": 0.0, "Fee": 0.0, "Net P/L": 0.0, "Reason": "INSUFFICIENT_MARGIN"})
                continue

            cycle_id += 1
            active = {"direction": direction, "base": base, "signal_time": t, "start_order_index": len(order_rows), "initial_price": base}
            fill_order("BUY" if direction == "BUY" else "SELL", base, GRID_INITIAL_CONTRACTS, "INITIAL_ENTRY", t, i)
            # Orders become active only from the next candle.
            continue

        if active is None:
            continue

        direction = active["direction"]
        base = active["base"]
        atr_line = float(row.SUPERTREND) if not pd.isna(row.SUPERTREND) else base

        if direction == "BUY":
            target_levels = [("TARGET", base + GRID_STEP * n, "SELL") for n in range(1, GRID_UPPER_LEVELS + 1)]
            lower_n = int((base - atr_line) // GRID_STEP) if atr_line < base else 0
            lower_levels = [("ATR", base - GRID_STEP * n, "BUY") for n in range(1, lower_n + 1) if base - GRID_STEP * n >= atr_line]
        else:
            target_levels = [("TARGET", base - GRID_STEP * n, "BUY") for n in range(1, GRID_UPPER_LEVELS + 1)]
            lower_n = int((atr_line - base) // GRID_STEP) if atr_line > base else 0
            lower_levels = [("ATR", base + GRID_STEP * n, "SELL") for n in range(1, lower_n + 1) if base + GRID_STEP * n <= atr_line]

        # Reconstruct currently open grid orders from fills: each target rung alternates target/re-entry.
        # A rung is active at target initially; after target fill, its re-entry is active.
        active_orders = []
        for label, level, side in target_levels + lower_levels:
            active_orders.append({"label": label, "level": level, "side": side})

        # We use cycle order history to determine the current leg for each target rung.
        # Lower ATR orders are one-way accumulation orders and remain active after fill.
        filled_events = order_rows[active["start_order_index"]:]
        for target_label, level, side in target_levels:
            relevant = [x for x in filled_events if x["Type"] in (target_label, "REENTRY") and abs(float(x["Price"]) - level) < 1e-8]
            # If target already filled, its re-entry is one step back toward base.
            if relevant:
                last = relevant[-1]
                if last["Type"] == target_label:
                    re_level = level - GRID_STEP if direction == "BUY" else level + GRID_STEP
                    active_orders = [o for o in active_orders if not (o["label"] == target_label and abs(o["level"]-level)<1e-8)]
                    active_orders.append({"label": "REENTRY", "level": re_level, "side": "BUY" if direction == "BUY" else "SELL"})
                else:
                    # re-entry filled -> target is armed again
                    pass

        # Process the candle's intrabar path. Prevent multiple fills of the same order in one path point.
        prev_p = float(row.open)
        filled_this_candle = set()
        for p, point_name in path_points(row, path_mode):
            if p == prev_p:
                prev_p = p
                continue
            for idx, o in list(enumerate(active_orders)):
                key = (o["label"], round(o["level"], 8), o["side"])
                if key in filled_this_candle:
                    continue
                if crossed(prev_p, p, o["level"], "UP") or crossed(prev_p, p, o["level"], "DOWN"):
                    ok, reason = fill_order(o["side"], o["level"], GRID_CHUNK_CONTRACTS, o["label"], t, i)
                    if ok:
                        filled_this_candle.add(key)
                        # Do not immediately fill the newly-created opposite leg in the same candle.
                        # It becomes active on a later candle, matching exchange-style order lifecycle.
            prev_p = p

        # mark equity after fills
        equity = cash + ((close - avg_entry) * position * CONTRACT_BTC if position else 0.0)
        equity_rows.append({"Time": t, "Equity": equity, "Cash": cash, "Position Contracts": position, "Price": close})
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, peak_equity - equity)

    if active is not None:
        last = df.iloc[-1]
        last_t = datetime.fromtimestamp(int(last.time), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        close_position(float(last.close), "END_OF_BACKTEST", last_t, len(df)-1)
        cycle_rows.append({"Cycle": cycle_id, "Direction": active["direction"], "Signal Time": active["signal_time"], "Signal Price": active["base"], "Close Time": last_t, "Close Price": float(last.close), "Reason": "END_OF_BACKTEST", "Net P/L": cycle_pnl(active, order_rows)})

    # Final mark-to-market / balance summary.
    last_price = float(df.iloc[-1].close)
    final_equity = cash + ((last_price - avg_entry) * position * CONTRACT_BTC if position else 0.0)
    net_profit = final_equity - starting_balance
    wins = sum(1 for x in order_rows if x["Net P/L"] > 0 and x["Type"] not in ("INITIAL_ENTRY",))
    losses = sum(1 for x in order_rows if x["Net P/L"] < 0 and x["Type"] not in ("INITIAL_ENTRY",))
    return {
        "orders": pd.DataFrame(order_rows),
        "cycles": pd.DataFrame(cycle_rows),
        "equity": pd.DataFrame(equity_rows).drop_duplicates("Time", keep="last"),
        "starting_balance": starting_balance,
        "final_equity": final_equity,
        "net_profit": net_profit,
        "max_drawdown": max_drawdown,
        "fees": fees_total,
        "realized": realized_total,
        "signals": signal_count,
        "blocked": blocked_count,
        "wins": wins,
        "losses": losses,
    }


def cycle_pnl(active, orders):
    return sum(float(x.get("Net P/L", 0)) for x in orders[active["start_order_index"]:] if x.get("Cycle") == next_cycle(orders, active))


def next_cycle(orders, active):
    vals = [x.get("Cycle") for x in orders[active["start_order_index"]:] if x.get("Cycle") is not None]
    return vals[0] if vals else 0


st.title("📊 Delta Exchange India — SuperTrend Grid Backtest")
st.caption("Historical Delta Exchange candles only • No live orders • Same 1H SuperTrend 10/3 + Grid rules from the uploaded trading file")

with st.sidebar:
    st.header("Backtest Settings")
    symbol = st.text_input("Delta Symbol", SYMBOL)
    days = st.selectbox("Historical period", [7, 14, 30, 60, 90, 180, 365], index=4)
    starting_balance = st.number_input("Starting balance (USD)", min_value=1.0, value=100.0, step=10.0)
    leverage = st.number_input("Leverage assumption (x)", min_value=1.0, value=100.0, step=1.0)
    fee_pct = st.number_input("Fee per filled order (%)", min_value=0.0, value=0.05, step=0.01, format="%.3f")
    fee_rate = fee_pct / 100.0
    entry_offset = st.number_input("Initial entry offset (points)", value=0, step=10)
    path_mode = st.selectbox("Intrabar fill path", ["AUTO", "OHLC", "OLHC"])
    run = st.button("▶ Run Backtest", type="primary", use_container_width=True)

st.info(f"Source: {BASE_URL} | Symbol: {symbol} | Product ID: {PRODUCT_ID} | Timeframe: {TIMEFRAME} | Contract: {CONTRACT_BTC} BTC | Grid: {GRID_STEP} points | Initial: {GRID_INITIAL_CONTRACTS} contracts")

if run or "bt_result" not in st.session_state:
    try:
        with st.spinner("Delta Exchange se historical candles download ho rahe hain..."):
            raw = fetch_candles(symbol, TIMEFRAME, days)
            if len(raw) < ATR_PERIOD + 5:
                st.error(f"Sirf {len(raw)} completed candles mile; SuperTrend ke liye aur data chahiye.")
                st.stop()
            data = calculate_supertrend(raw)
            result = run_backtest(data, starting_balance, leverage, fee_rate, int(entry_offset), path_mode)
            st.session_state.bt_data = data
            st.session_state.bt_result = result
    except Exception as e:
        st.error(f"Backtest failed: {e}")
        st.stop()

result = st.session_state.bt_result
data = st.session_state.bt_data

c1,c2,c3,c4,c5,c6 = st.columns(6)
c1.metric("Starting USD", f"${result['starting_balance']:,.2f}")
c2.metric("Final Equity", f"${result['final_equity']:,.2f}")
c3.metric("Net P/L", f"${result['net_profit']:,.2f}")
c4.metric("Max Drawdown", f"${result['max_drawdown']:,.2f}")
c5.metric("Filled/Recorded Orders", str(len(result['orders'])))
c6.metric("Signals", str(result['signals']))

c7,c8,c9,c10 = st.columns(4)
c7.metric("Fees", f"${result['fees']:,.4f}")
c8.metric("Blocked Entries", str(result['blocked']))
c9.metric("Positive Fills", str(result['wins']))
c10.metric("Negative Fills", str(result['losses']))

st.subheader("💰 Minimum Starting Balance Test")
rows=[]
for bal in [10,20,50,100,200,500,1000]:
    r = run_backtest(data, bal, leverage, fee_rate, int(entry_offset), path_mode)
    rows.append({"Starting USD":bal,"Final Equity":r["final_equity"],"Net P/L":r["net_profit"],"Max Drawdown":r["max_drawdown"],"Signals":r["signals"],"Blocked Entries":r["blocked"],"Fees":r["fees"]})
st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.subheader("📈 Equity Curve")
eq = result["equity"].copy()
if not eq.empty:
    st.line_chart(eq.set_index("Time")["Equity"])

st.subheader("🧾 Complete Order Record")
orders = result["orders"].copy()
if not orders.empty:
    st.dataframe(orders, use_container_width=True, hide_index=True)
    st.download_button("⬇ Download Order CSV", orders.to_csv(index=False).encode(), "delta_backtest_orders.csv", "text/csv")
else:
    st.info("No orders recorded.")

st.subheader("🔄 Cycle / Trade Summary")
cycles = result["cycles"].copy()
if not cycles.empty:
    st.dataframe(cycles, use_container_width=True, hide_index=True)
    st.download_button("⬇ Download Cycle CSV", cycles.to_csv(index=False).encode(), "delta_backtest_cycles.csv", "text/csv")
else:
    st.info("No completed cycles.")

st.subheader("🕯 Historical Delta Candles + SuperTrend Signals")
show = data[["time","open","high","low","close","SUPERTREND","ST_DIRECTION","SIGNAL"]].copy()
show["time"] = pd.to_datetime(show["time"], unit="s", utc=True)
st.dataframe(show.tail(300), use_container_width=True, hide_index=True)
st.download_button("⬇ Download Historical Candle/Signal CSV", show.to_csv(index=False).encode(), "delta_historical_supertrend.csv", "text/csv")

st.caption("Important: candle OHLC does not reveal the exact intrabar order in which multiple price levels were touched. The selected Intrabar fill path is therefore shown in the settings and should be kept consistent when comparing runs.")
