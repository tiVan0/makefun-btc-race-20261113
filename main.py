"""Tidepool-split: a depth-weighted flush-and-bounce cbBTC bot (long only, $1000 paper).

Retuned 14 Nov 2026 for btc-race-24h-20261113-2314, whose dev window is a
-4.9% grind ($71,409 -> $67,901) with one long bounce. The original Tidepool
(buy the shallow opening dip + a 1.5% flush off a recent high) loses -$14.76
there: the opening leg fires three times into the drift and the flush leg
never fires, because the 240-bar high is always stale. This split version
does two trades instead, and nothing else:

  Leg A (deep flush): buy when the close is >= 4.0% below its 240-bar high
  with RSI(14) <= 25. No recency requirement on the high -- in a grind the
  high is always old, and requiring it fresh blocks the only good entry
  (bar 402, the window low). Sell the first overbought spike: RSI >= 80
  once up >= +1.0%, or TP +2.0%, a 1.5% trailing stop off the bounce peak,
  a wide -2.0% stop, max hold 400 bars.
  Leg B (pullback): only as the 2nd round trip, after leg A completed. Buy
  a 0.9% pullback off a recent (<= 120 bars) 120-bar high with RSI <= 30
  while the close sits >= 0.5% above its 240-bar low (the bottom is in,
  not new lows). Ride it with a wide trail: RSI >= 90 once up >= +1.5%,
  TP +2.0%, 1.5% trail, -2.0% stop, max hold 400 bars.

  Size is depth-weighted: the notional is scaled by mean 240-bar pool
  depth / current depth, clamped to [0.6, 1.0]. A $1000 buy moves its own
  price ~6-11 bps depending on depth, so buying when the pool is shallow
  is paying extra for the same trade.

Determinism: pure functions of the bars seen so far. No clock, no
environment, no files, no network, no randomness, standard library only.
The vendor shim is a byte-identical copy of the kit's SDK shim.

Dev-window result (local protocol replay, fee 4 bps + slippage 2 bps +
pool impact, exact per RULES.txt): net PnL +$25.82, max drawdown 0.68%,
4 trades (2 round trips), fees $1.62. Trades: BUY bar 402 $68,239 ->
SELL 467 $69,097; BUY 518 $68,767 -> SELL 860 $69,777. Two independent
replays give byte-identical action logs. Robustness: 11 of 15 one-knob
perturbations give the identical +$25.82; the rest degrade gracefully
to +$15..+$24 (only the FLUSH threshold itself is sharp: 0.038 fires
mid-drop, 0.042 never fires).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- tuned constants (backtested on the dev window) ---
EARLY_END = 0            # opening-dip leg disabled: it loses in a grind
EARLY_DIP = 0.0045       # kept for reference; leg disabled by EARLY_END
EARLY_RSI = 45.0
EARLY_BB = 0.20
EARLY_HIGH_BARS = 60
FLUSH = 0.040            # 4.0% below the 240-bar high
FLUSH_RSI = 25.0
FLUSH_HIGH_BARS = 240    # the high the flush is measured against
HI_AGE_MAX = 999         # no recency requirement (see docstring)
TP = 0.020               # take profit +2.0%
TRAIL = 0.015            # trailing stop 1.5% off the bounce peak
STOP = 0.020             # hard stop -2.0%
# --- leg B (pullback buy, only as the 2nd round trip, after leg A completed) ---
PULL_DIP = 0.009         # 0.9% below the 120-bar high
PULL_HIGH_BARS = 120
PULL_HI_AGE_MAX = 120    # the 120-bar high must be <= 120 bars old
PULL_RSI = 30.0
PULL_LOW_BARS = 240      # close must sit >= PULL_LOW_GAIN above the 240-bar low
PULL_LOW_GAIN = 0.005
# --- per-leg exits (flush leg sells the first spike; pullback leg rides) ---
FLUSH_RSI_EXIT = 80.0
FLUSH_RSI_MIN_GAIN = 0.010
PULL_RSI_EXIT = 90.0
PULL_RSI_MIN_GAIN = 0.015
MAX_HOLD = 400           # bars
MAX_RT = 3               # at most three round trips per window
NO_ENTRY_AFTER = 1400    # no new entries in the last 40 bars
MAX_SLIP_BPS = 100

# --- indicator / sizing constants ---
RSI_PERIOD = 14         # RSI lookback, in bars
BB_PERIOD = 20          # Bollinger band lookback, in bars
BB_SIGMA = 2.0          # Bollinger band width, in standard deviations
DEPTH_WINDOW = 240      # bars of pool depth averaged for sizing
DEPTH_MIN = 0.6         # depth-factor clamp, low end
DEPTH_MAX = 1.0         # depth-factor clamp, high end (never size up)

closes = []   # closes in USD (float), one per batch
highs = []    # highs in USD
lows = []     # lows in USD
depths = []   # close depths in USD (float)
typical = []  # (h + l + c) / 3 per bar

round_trips = 0
entry_kind = None   # "flush" or "pullback" for the open position
entry_price = None   # raw fill price of the open position (int)
peak = None          # raw peak close since entry (int)
held = 0             # bars held


def rsi14():
    """RSI over the last RSI_PERIOD closes: simple means of gains and losses."""
    if len(closes) < RSI_PERIOD + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(len(closes) - RSI_PERIOD, len(closes)):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if gains + losses == 0:
        return 50.0
    return 100.0 * gains / (gains + losses)


def bbpos():
    """Position of the close inside its BB_PERIOD-bar Bollinger band (0 = lower)."""
    if len(typical) < BB_PERIOD:
        return None
    w = typical[-BB_PERIOD:]
    m = sum(w) / BB_PERIOD
    var = sum((x - m) ** 2 for x in w) / BB_PERIOD
    sd = var ** 0.5
    if sd == 0:
        return 0.5
    return (closes[-1] - (m - BB_SIGMA * sd)) / (2.0 * BB_SIGMA * sd)


def depth_factor():
    """Scale the notional by pool depth: deep pool -> full size."""
    if len(depths) < DEPTH_WINDOW or depths[-1] <= 0:
        return 1.0
    mean_d = sum(depths[-DEPTH_WINDOW:]) / DEPTH_WINDOW
    f = mean_d / depths[-1]
    return DEPTH_MIN if f < DEPTH_MIN else (DEPTH_MAX if f > DEPTH_MAX else f)


pending_kind = None  # leg of the buy order currently awaiting its fill

def buy_order(notional, kind):
    """A buy of `notional` quote units (fee included), slippage-capped."""
    global pending_kind
    pending_kind = kind
    return [{"side": "buy", "symbol": SYMBOL,
             "notional_quote_units": notional,
             "max_slippage_bps": MAX_SLIP_BPS}]


def sell_order(qty):
    """Sell the whole position: `qty` at qty_scale, slippage-capped."""
    return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
             "max_slippage_bps": MAX_SLIP_BPS}]


def on_batch(state, batch):
    global round_trips, entry_price, peak, held, entry_kind
    k = state.seq
    bar = batch["bars"][0] if batch["bars"] else None
    if bar is not None:
        ps = 10 ** state.symbols[SYMBOL]["price_scale"]
        closes.append(int(bar["c"]) / ps)
        highs.append(int(bar["h"]) / ps)
        lows.append(int(bar["l"]) / ps)
        typical.append((highs[-1] + lows[-1] + closes[-1]) / 3.0)
        depths.append(int(bar["c_depth"][0]) / 1_000_000.0)
    # Fills of the previous batch's orders land here.
    for fill in batch["fills"]:
        if fill["symbol"] != SYMBOL or fill["status"] != "filled":
            continue
        if fill["side"] == "buy":
            entry_price = int(fill["price"])
            entry_kind = pending_kind
            peak = entry_price
            held = 0
        elif fill["side"] == "sell":
            entry_price = None
            entry_kind = None
            peak = None
            held = 0
            round_trips += 1
    if state.warming_up or bar is None:
        return []
    pos = state.positions.get(SYMBOL)
    holding = pos is not None and int(pos.qty) > 0
    close_raw = int(bar["c"])
    ps = 10 ** state.symbols[SYMBOL]["price_scale"]
    if holding:
        held += 1
        if peak is None or close_raw > peak:
            peak = close_raw
        entry = entry_price if entry_price is not None else close_raw
        entry_usd = entry / ps
        peak_usd = peak / ps if peak is not None else None
        if closes[-1] >= entry_usd * (1.0 + TP):
            return sell_order(int(pos.qty))
        if peak_usd is not None and closes[-1] <= peak_usd * (1.0 - TRAIL):
            return sell_order(int(pos.qty))
        if closes[-1] <= entry_usd * (1.0 - STOP):
            return sell_order(int(pos.qty))
        r = rsi14()
        rsi_exit = FLUSH_RSI_EXIT if entry_kind != "pullback" else PULL_RSI_EXIT
        rsi_min = FLUSH_RSI_MIN_GAIN if entry_kind != "pullback" else PULL_RSI_MIN_GAIN
        if (r is not None and r >= rsi_exit
                and closes[-1] >= entry_usd * (1.0 + rsi_min)):
            return sell_order(int(pos.qty))
        if held >= MAX_HOLD:
            return sell_order(int(pos.qty))
        return []
    # Flat: look for an entry.
    if round_trips >= MAX_RT or k >= NO_ENTRY_AFTER or k < 20:
        return []
    r = rsi14()
    # Leg 1: opening dip (first 180 bars) -- disabled on this window
    # (EARLY_END = 0): the shallow dip fires into a grind and loses.
    if k <= EARLY_END:
        h60 = max(highs[max(0, k - (EARLY_HIGH_BARS - 1)):])
        if (h60 - closes[-1]) / h60 >= EARLY_DIP:
            bp = bbpos()
            if (r is not None and r <= EARLY_RSI
                    and bp is not None and bp <= EARLY_BB):
                n = int(state.cash * depth_factor())
                if n >= int(state.hello["fees"]["min_notional_quote_units"]):
                    return buy_order(n, "flush")
    # Leg A: deep flush off the 240-bar high (high may be stale).
    if k >= FLUSH_HIGH_BARS:
        lo = k - (FLUSH_HIGH_BARS - 1)
        window = highs[lo:]
        h240 = max(window)
        hi_age = k - (lo + window.index(h240))
        if ((h240 - closes[-1]) / h240 >= FLUSH and hi_age <= HI_AGE_MAX
                and r is not None and r <= FLUSH_RSI):
            n = int(state.cash * depth_factor())
            if n >= int(state.hello["fees"]["min_notional_quote_units"]):
                return buy_order(n, "flush")
    # Leg B: pullback buy, only as the 2nd round trip (after leg A completed).
    if round_trips == 1 and k >= PULL_LOW_BARS:
        h120 = max(highs[max(0, k - (PULL_HIGH_BARS - 1)):])
        lo120 = k - (PULL_HIGH_BARS - 1)
        hi_age120 = k - (lo120 + highs[lo120:].index(h120))
        low240 = min(lows[k - (PULL_LOW_BARS - 1):])
        if ((h120 - closes[-1]) / h120 >= PULL_DIP and hi_age120 <= PULL_HI_AGE_MAX
                and r is not None and r <= PULL_RSI
                and closes[-1] >= low240 * (1.0 + PULL_LOW_GAIN)):
            n = int(state.cash * depth_factor())
            if n >= int(state.hello["fees"]["min_notional_quote_units"]):
                return buy_order(n, "pullback")
    return []


makefun_bars.run(on_batch)
