# Feature and quality contracts

Core identity is canonical_asset_id; exchange IDs and metadata remain visible. Quote validity is independent of technical-indicator readiness: a fresh valid quote can measure outcomes even when Kline/depth is absent. Features carry component_times, units, scope and missing metrics. Source future timestamps, gaps, stale endpoints or incompatible units prevent advanced confirmation.

Phase 1: 1m closed-candle momentum, relative volume, acceleration, ATR/RSI/EMA and sampled depth; spot trade taker direction and sampling coverage; UTC-aligned BTC and ETH relative strength; OKX current OI/USD OI plus documented 5m OI history; funding and settlement history, mean/Z-score/percentile; contract taker unit=2 (USD notional) ratios/delta; retail/top-trader account and position ratios; mark/index basis plus explicitly separate cross-venue premium. Contract books are converted with metadata ctVal/ctMult/ctValCcy only for supported linear contracts. Unsupported conversions remain missing. Trades use unique trade IDs and sampled-window CVD labels, never claim complete continuous flow from one recent-trades sample.

Cross-section rank uses only eligible current altcoin feature inputs. Missing components reduce coverage and confidence; absent values never count as zero evidence. Each ranking batch saves prior rank/score, delta and score contribution changes. Scores are rule evidence scores, not calibrated probabilities. Spot/perp flow magnitudes are normalized independently; raw cross-exchange volume is never directly compared.

Actual liquidation amount remains UNAVAILABLE. A separately named deleveraging_signal is an evidence hypothesis, never called actual liquidation volume or a calibrated probability.
