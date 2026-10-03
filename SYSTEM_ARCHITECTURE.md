# V4 architecture and repository audit

Source: [V4 plan](docs/requirements/v4-latest-plan.md), sections 0–120. Phase 1 is the implementation scope of this change. V4 supersedes the earlier 100-total target: BTC + ETH are core assets, plus 100 dynamic altcoins. Telegram is excluded. DEX discovery belongs to Phase 4; historical records stay accessible and no GoPlus collection is restored in Phase 1.

Baseline b1b5004 has SQLite immutable raw/snapshot/signal history, FastAPI, a combined provider, BTC rules and archived replay. Gaps: canonical registry, durable universe, ETH core status, OKX history/taker/ratios, ranking, persistent web alert lifecycle and production packaging. The 10-minute local OI sampling problem and exclusion of quote-only snapshots from performance measurement must be fixed.

Pipeline: transport with timeout/backoff/rate gates -> BinanceSpotProvider / OKXDerivativesProvider -> canonical instrument registry -> UTC and unit validation -> immutable raw observations -> feature snapshots -> BTC regime / cross-section ranking -> evidence signals -> durable Web Alert Center -> forward measurement / archived replay. Business rules never call HTTP endpoints. Snapshot evidence carries per-component source and receive timestamps; missing/stale components are excluded from advanced alerts.

SQLite remains the Phase 1 backend to preserve the existing database and keep this instance runnable. Additive versioned migrations, persistent universe and ranking tables prepare a later PostgreSQL/time-series backend; distributed production scaling is not claimed. Current registry matching is by exact exchange base/quote and explicit metadata; chain identity stays unverified unless documented, and symbol prefixes are never guessed.
