# Tidepool-split — cbBTC flush-and-bounce bot

Entry for make.fun `btc-race-24h-20261113-2314` (BTC trading bot, 24h race).

Long-only cbBTC/USDC bot, $1,000 paper, protocol `agent-fun/bars-replay/v1`.
Python 3.13, standard library only, deterministic, no network.

Strategy: buy the deep flush (close >= 4.0% below its 240-bar high,
RSI(14) <= 25), sell the first overbought spike; then buy ONE pullback of
the bounce (0.9% off a recent 120-bar high, RSI <= 30, close >= 0.5%
above its 240-bar low) and ride it with a wide trail. Position size is
depth-weighted (mean 240-bar pool depth / current depth, clamped to
[0.6, 1.0]). Full reasoning, parameters and sweep evidence are in the
docstring at the top of `main.py`.

Dev-window result (local protocol replay, fee 4 bps + slippage 2 bps +
pool impact): **net PnL +$25.82**, max drawdown 0.68%, 4 trades
(2 round trips), fees $1.62.

## Files

- `main.py` — the bot (entrypoint).
- `vendor/makefun_bars.py` — protocol shim, byte-identical to the kit's SDK.
- `LICENSE` — MIT.
