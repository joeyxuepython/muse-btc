# V4 implementation and acceptance

Authoritative source: [complete V4](docs/requirements/v4-latest-plan.md). Start with repo audit/design and official capability verification; then implement Phase 1 autonomously. Original configuration, data and local documentation are preserved. Do not merge the review branch or change Binance proxy configuration as part of this work.

Phase 1 acceptance: CORE BTC/ETH + 100 dynamic altcoins; exact registry and daily bounded turnover/pins; Binance spot quotes/details and OKX history/taker/ratios; additive storage and persistent membership; tiered collection and data health; rank/score history; explained signals and durable Web Alert Center; forward measurements/replay; packaging, backup and local integration validation. Verify real response shape and timestamps; report unavailable metrics and coverage. Long unattended availability and investment efficacy require elapsed live operation, not short smoke tests.

Phase 2: macro, ETF/stablecoin, authorized email and public research, advanced cross-venue/CVD statistics. Phase 3: richer fusion/catalyst/tokenomics/rank dynamics. Phase 4: DEX/RPC Meme risk, holders and wallets. Phase 5: social/narratives. Phase 6: advanced backtests, ML, calibration and evaluated paid sources. Telegram remains excluded throughout.

Record implementation and verification outcomes here at completion; unsupported source access is a blocker for the dependent feature, not a reason to create mock production data.

## Completed acceptance — 2026-10-03

Scope is V4 section104/Phase1 plus the required section115 design audit. See [chapter-by-chapter status](docs/v4-requirements-audit.md) for every remaining scope group. The full V4 is not complete.

| Check | Actual result | Limit |
| --- | --- | --- |
| Python lint/format + pytest + JS syntax | 62 tests pass; ruff and node checks pass | Tests use labelled contracts and isolated databases; they do not measure prediction efficacy |
| Live public exchange collection | Two final cycles at 03:55:36 / 03:55:53 UTC returned 102/102 quotes and 100 rankings each | Manual cycles run immediately, not a 24/7 soak test |
| Provider restart | Persistent round1→2, universe102 retained, detailed assets22→42 | Default120-second polling yields lower fresh detail coverage in lower tiers |
| Current OKX OI | 102/102 in both final live cycles | Supported current OI is not full history support |
| Funding / USD taker | First cycle22/21; second42/39 | Some instrument statistics absent; remaining details await rotation |
| Real alerts | One actual signal/Web alert in final temporary run | Observation only; forward windows have not matured |
| API against live temporary history | ready200, assets102, rankings100, alerts1, derivatives_ready=false | Ready means target quotes and core technical readiness; complete derivatives coverage is separate |
| Container | Non-root image built with locked dependencies and hashes; health200, unauthenticated401, authenticated200, real WebSocket update, static files200 | Collector disabled for container smoke; no original database mounted |
| Packaging / Compose | Wheel build succeeds; cloud overlay compose config validates | Long unattended deployment not performed |
| Preservation | Original DB still schema1: raw14370, snapshots8504, signals248, outcomes375, regimes522; SQLite backup integrity=ok | Original runtime/database not upgraded in place during development |

[Machine-readable live results](docs/verification/v4-live-acceptance.json) retain the observed UTC times and coverage. Reproduce with `.venv/bin/python scripts/verify_live.py`; it creates a new temporary database and never opens `data/muse.db`. The original DB backup is `/tmp/muse-pre-v4-history-backup.db` in this cloud instance. Do not commit data or credentials.

Missing metrics, stale/unavailable components, unsupported contract conversions, unverified chain identity and insufficient future samples remain explicit. Continuous CVD/lead-lag, complete liquidation totals, macro/research/mailbox, full pre-pump hypotheses, Top-K performance, lead-time calibration, social/on-chain intelligence and ML remain later work. Telegram is excluded.
