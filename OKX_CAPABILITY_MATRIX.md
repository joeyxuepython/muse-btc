# OKX capability matrix

Official source: https://www.okx.com/docs-v5/en/ . Verified 2026-10-03 against current official documentation and actual public responses. All enabled endpoints are unauthenticated. AVAILABLE means a successful sample, not complete coverage for every instrument. “Not marked” means the official page does not mark this endpoint deprecated.

| Metric | Official endpoint | Auth | Rate limit and scope | History / page limit | Available | Deprecated | Verified date |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Instrument metadata | `/api/v5/public/instruments` | Public | 20/2s; IP + instrument type | Current | AVAILABLE | Not marked | 2026-10-03 |
| Contract tickers | `/api/v5/market/tickers` | Public | 20/2s; IP | Current rolling 24h | AVAILABLE | Not marked | 2026-10-03 |
| Open Interest | `/api/v5/public/open-interest` | Public | 20/2s; IP + instrument ID | Current | AVAILABLE | Not marked | 2026-10-03 |
| OI History | `/api/v5/rubik/stat/contracts/open-interest-history` | Public | 10/2s; IP + instrument ID | Latest 1440 entries; 100/page; boundary depends on period | AVAILABLE | Not marked | 2026-10-03 |
| Funding | `/api/v5/public/funding-rate` | Public | 10/2s; IP + instrument ID | Current estimate / upcoming settlement | AVAILABLE | Not marked | 2026-10-03 |
| Funding History | `/api/v5/public/funding-rate-history` | Public | 10/2s; IP + instrument ID | Up to 3 months; 400/page | AVAILABLE | Not marked | 2026-10-03 |
| Mark Price | `/api/v5/public/mark-price` | Public | 10/2s; IP + instrument ID | Current | AVAILABLE | Not marked | 2026-10-03 |
| Index Price | `/api/v5/market/index-tickers` | Public | 20/2s; IP | Current | AVAILABLE | Not marked | 2026-10-03 |
| Basis | Derived `mark/index - 1`; unit % | Derived | No additional endpoint | Aligned point in time | AVAILABLE (derived) | n/a | 2026-10-03 |
| Taker Buy/Sell | `/api/v5/rubik/stat/taker-volume-contract` | Public | 5/2s; IP + instrument ID | Latest 1440 entries; 100/page | AVAILABLE; some selected instruments lack rows | Not marked | 2026-10-03 |
| Long/Short Ratio | `/api/v5/rubik/stat/contracts/long-short-account-ratio-contract` | Public | 5/2s; IP + instrument ID | Latest 1440 entries; 100/page | AVAILABLE | Not marked | 2026-10-03 |
| Elite Trader Ratio | `/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader` | Public | 5/2s; IP + instrument ID | Latest 1440 entries; 100/page; documented start 2024-03-22 | AVAILABLE | Not marked | 2026-10-03 |
| Elite Position Ratio | `/api/v5/rubik/stat/contracts/long-short-position-ratio-contract-top-trader` | Public | 5/2s; IP + instrument ID | Latest 1440 entries; 100/page | AVAILABLE | Not marked | 2026-10-03 |
| Contract Order Book | `/api/v5/market/books` | Public | 40/2s; IP | Current; up to 400 depth levels | AVAILABLE | Not marked | 2026-10-03 |
| Contract Trades | `/api/v5/market/trades` | Public | 100/2s; IP | Recent sample; up to 500 | AVAILABLE | Not marked | 2026-10-03 |
| Contract Candles | `/api/v5/market/candles` | Public | 40/2s; IP | Recent 1440 candles; 300/page | AVAILABLE | Not marked | 2026-10-03 |
| Actual liquidation total | No supported REST total enabled | n/a | n/a | No complete total | UNAVAILABLE | n/a | 2026-10-03 |
| Liquidation WS sample | Public WS `liquidation-orders` channel | Public WS | See official WS restrictions | Partial live stream; no complete total | NEEDS_VERIFICATION; not enabled | Not marked | 2026-10-03 |

The WS liquidation page explicitly states it does not represent total liquidations. A separately named deleveraging hypothesis never becomes actual liquidation volume or calibrated probability.

OI history arrays are `[ts, oi contracts, oiCcy coin, oiUsd USD]`; 5m changes use adjacent 300-second contract-count points. Taker history uses `unit=2` (USD), arrays `[ts, sellVol, buyVol]`. Ratios use `[ts, ratio]`. Some new/low-activity instruments have no historical rows; keep missing values explicit.

Funding `ts` is generation time; `fundingTime` is the future settlement time and must not be used as availability. History `realizedRate` is settled actual rate; history `fundingRate` may be predicted. Elite ratios are the top 5% by position value, not identified smart-money wallets.

SWAP ticker `volCcy24h` is base currency. Universe scoring converts it with that contract's last USDT price to an explicitly labelled notional proxy before cross-asset ranking. It is not exact traded turnover or directly compared to Binance raw flow. Contract book/trade quantities use documented `ctVal/ctMult/ctValCcy` metadata for supported linear conversions; unsupported identity/conversion remains missing.

No current API support assertion replaces monitoring. Refer to [acceptance](IMPLEMENTATION_PLAN.md) for actual partial live coverage.
