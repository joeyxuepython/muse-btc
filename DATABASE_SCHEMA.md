# Phase 1 schema and preservation

Existing raw_observations, snapshots, signals, signal_events, outcomes, provider_status, regimes, derivatives_oi stay readable. SQLite WAL, UTC ISO timestamps, transactional writes and additive PRAGMA user_version migration preserve existing JSON payloads and .env. New optional Pydantic fields default safely for older records.

New tables: instrument_registry(canonical_asset_id, payload), universe_history(id, selected_at, payload), ranking_history(batch_id, as_of, asset_id, payload), web_alerts(id, asset_id, rule_id, last_updated, payload; level, first_seen, expires_at, reader state and lifecycle fields within payload), web_alert_events(id, alert_id, event_at, payload), runtime_state(key, payload). Rankings and universe use immutable batches; alert row is current lifecycle view plus immutable event history. The single collector maintains one active asset/rule lifecycle and supports escalation rather than creating independent WATCH/SETUP/STRONG rows. Reader status/pins do not modify signal or raw evidence.

Back up with SQLite backup API before running a migration on the existing data file. No destructive retention defaults. Raw tick retention configuration is reserved; cleanup requires an explicit supported retention job and does not delete signal/feature/universe lineage. Export and read-only replay never mutate historical observations. PostgreSQL/Timescale/Redis are future backend options, not required services hidden behind this implementation.

## Schema 4 reliability migration

Before upgrading versions 1–3 the application creates a `.pre-v4.bak` using SQLite backup. The intelligence table preserves IDs but removes the global `(kind,key,content_hash)` uniqueness constraint: only an unchanged latest revision is deduplicated. `evidence_checks(record_id,checked_at,raw_ids)` keeps subsequent successful observations without changing initial availability, while `decision_configs(as_of,payload)` stores an allowlisted configuration without credentials.

New raw payloads longer than 4096 characters are gzip/base64 encoded with a `gzip:` marker; `Store.raw()` transparently decodes them and the hash always represents original JSON. Roll back with the pre-upgrade backup at a separate path, not old code against newly compressed data. No history is automatically deleted. Operational steps: [reliability upgrade](docs/reliability-upgrade.md).
