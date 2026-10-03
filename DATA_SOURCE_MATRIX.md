# Data source matrix

Verified 2026-10-03. PUBLIC means no account authentication; endpoint availability is distinct from complete coverage. Official sources: [Binance](https://developers.binance.com/docs/binance-spot-api-docs/rest-api), [OKX](https://www.okx.com/docs-v5/en/).

| Provider | Data | Cost | Transport/history | Frequency | Limits/auth | Fallback/status |
| --- | --- | --- | --- | --- | --- | --- |
| Binance Spot | metadata, 24h quotes, candles, sampled depth, trades | Free | REST; candles plus local raw history | quotes every cycle; metadata/universe daily; tiered detail | IP request weights; no auth | configured official read-only URL; PUBLIC; two real cycles returned 102/102 quotes |
| OKX Derivatives | metadata, tickers/marks/OI, funding/history, OI history, taker, ratios, candles/books/trades | Free | REST; metric history depends on documented endpoint | bulk OI every cycle, details by tier | endpoint/IP or IP+instId limits; no auth | missing metrics UNKNOWN; verified matrix below |
| Macro | releases, revisions, consensus, event response | Public sources planned | REST/public pages; point-in-time revision history | Phase 2 | sources and terms NEEDS_VERIFICATION | DISABLED |
| Research | public reports, provenance and dated metrics | Free public content planned | public pages/documents | Phase 2 | NEEDS_VERIFICATION | DISABLED |
| Email | newsletters and structured evidence | Account access required | authorized mailbox/import | Phase 2 | not configured | DISABLED |
| DEX | pool/token discovery | Public sources planned | RPC/public DEX | Phase 4 | NEEDS_VERIFICATION | DISABLED; historical records readable |
| On-chain | wallet/holder graphs and safety | Public sources planned | RPC/explorer | Phase 4 | NEEDS_VERIFICATION | DISABLED |
| Social | public narratives and quality | cost/terms vary | API/public content | Phase 5 | NEEDS_VERIFICATION | DISABLED |

[OKX capabilities](OKX_CAPABILITY_MATRIX.md) gives official endpoints, field units and observed availability. No fallback creates invented market data.

Binance official REST weights verified 2026-10-03 against [the official repository](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md): exchangeInfo=20, ticker/24hr with 102 symbols=80, depth limit100=5, aggTrades=4, klines=2. The default 22 detailed assets contribute 242 weight per cycle; the normal quote+detail round is 322 weight before daily metadata. Runtime request limits come from exchangeInfo; 418/429 and Retry-After trigger source backoff. This is a single-instance collector, not a shared IP-wide rate coordinator.
