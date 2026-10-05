import base64
import gzip
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import Outcome, ProviderStatus, Regime, Signal, SignalEvent, Snapshot, new_id


def stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Timestamps must have a timezone")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 4:
                raise ValueError("Database schema is newer than this application; upgrade the app")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version in (1, 2, 3):
                self.backup(path.with_suffix(path.suffix + ".pre-v4.bak"))
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS raw_observations (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, endpoint TEXT NOT NULL,
                    received_at TEXT NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, module TEXT NOT NULL,
                    market_time TEXT NOT NULL, available_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS snapshots_asset_time
                    ON snapshots(asset_id, available_at);
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, rule_id TEXT NOT NULL,
                    kind TEXT NOT NULL, emitted_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS signals_asset_time ON signals(asset_id, emitted_at);
                CREATE TABLE IF NOT EXISTS signal_events (
                    id TEXT PRIMARY KEY, signal_id TEXT NOT NULL,
                    event_at TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_signal_time
                    ON signal_events(signal_id, event_at);
                CREATE TABLE IF NOT EXISTS outcomes (
                    signal_id TEXT NOT NULL, horizon_seconds INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (signal_id, horizon_seconds)
                );
                CREATE TABLE IF NOT EXISTS provider_status (
                    name TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS derivatives_oi (
                    asset_id TEXT NOT NULL, ts TEXT NOT NULL, oi_usd REAL NOT NULL,
                    PRIMARY KEY (asset_id, ts)
                );
                CREATE INDEX IF NOT EXISTS derivatives_oi_asset_time
                    ON derivatives_oi(asset_id, ts);
                CREATE TABLE IF NOT EXISTS regimes (
                    as_of TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS instrument_registry (
                    canonical_asset_id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS universe_history (
                    id TEXT PRIMARY KEY, selected_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ranking_history (
                    batch_id TEXT NOT NULL, as_of TEXT NOT NULL, asset_id TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY (batch_id, asset_id)
                );
                CREATE INDEX IF NOT EXISTS ranking_asset_time
                    ON ranking_history(asset_id, as_of);
                CREATE TABLE IF NOT EXISTS web_alerts (
                    id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, rule_id TEXT NOT NULL,
                    last_updated TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS alerts_asset_rule
                    ON web_alerts(asset_id, rule_id);
                CREATE INDEX IF NOT EXISTS alerts_updated ON web_alerts(last_updated DESC,id);
                CREATE INDEX IF NOT EXISTS alerts_level_updated
                    ON web_alerts(json_extract(payload,'$.level'),last_updated DESC,id);
                CREATE TABLE IF NOT EXISTS web_alert_events (
                    id TEXT PRIMARY KEY, alert_id TEXT NOT NULL,
                    event_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS alert_events_alert_time
                    ON web_alert_events(alert_id,event_at);
                CREATE TABLE IF NOT EXISTS alert_notifications (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    notification_id TEXT NOT NULL UNIQUE, alert_id TEXT NOT NULL,
                    event_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_receipts (
                    notification_id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS notification_signal_lookup ON alert_notifications(
                    json_extract(payload,'$.signal_id'),event_at
                );
                CREATE TABLE IF NOT EXISTS strategy_runs (
                    batch_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS strategy_run_time ON strategy_runs(as_of);
                CREATE TABLE IF NOT EXISTS strategy_latest (
                    asset_id TEXT NOT NULL, rule_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(asset_id,rule_id)
                );
                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intelligence_records (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL,
                    market_time TEXT NOT NULL, available_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS intelligence_pit
                    ON intelligence_records(kind,key,available_at);
                CREATE INDEX IF NOT EXISTS intelligence_market_time
                    ON intelligence_records(kind,market_time,available_at);
                CREATE TABLE IF NOT EXISTS research_checks (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, checked_at TEXT NOT NULL,
                    succeeded INTEGER NOT NULL, message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_alerts (
                    document_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trade_observations (
                    venue TEXT NOT NULL, asset_id TEXT NOT NULL, trade_id TEXT NOT NULL,
                    market_time TEXT NOT NULL, received_at TEXT NOT NULL,
                    signed_notional REAL NOT NULL, raw_id TEXT NOT NULL,
                    PRIMARY KEY(venue,asset_id,trade_id)
                );
                CREATE INDEX IF NOT EXISTS trades_asset_time
                    ON trade_observations(venue,asset_id,market_time);
                CREATE TABLE IF NOT EXISTS collection_leases (
                    name TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence_checks (
                    record_id TEXT NOT NULL, checked_at TEXT NOT NULL, raw_ids TEXT NOT NULL,
                    PRIMARY KEY(record_id,checked_at)
                );
                CREATE TABLE IF NOT EXISTS decision_configs (
                    as_of TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
            """)
            if version in (1, 2, 3):
                # Preserve every evidence ID and all lineage while allowing A -> B -> A.
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE intelligence_v4 (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL,
                        market_time TEXT NOT NULL, available_at TEXT NOT NULL,
                        content_hash TEXT NOT NULL, payload TEXT NOT NULL
                    );
                    INSERT INTO intelligence_v4 SELECT * FROM intelligence_records;
                    DROP TABLE intelligence_records;
                    ALTER TABLE intelligence_v4 RENAME TO intelligence_records;
                    CREATE INDEX intelligence_pit ON intelligence_records(kind,key,available_at);
                    CREATE INDEX intelligence_market_time
                        ON intelligence_records(kind,market_time,available_at);
                    COMMIT;
                """)
            db.execute("PRAGMA user_version=4")
            # Seed once, in the same transaction as the durable generation marker.
            # Old alert rows cannot reconstruct historical escalation prices.
            initialized = db.execute(
                "INSERT OR IGNORE INTO runtime_state VALUES (?,?)",
                ("alert_notification_feed", json.dumps({"generation": new_id()})),
            ).rowcount
            if initialized:
                rows = db.execute(
                    "SELECT payload FROM web_alerts WHERE json_extract(payload,'$.level') "
                    "IN ('STRONG','CRITICAL_RISK') ORDER BY last_updated,id"
                ).fetchall()
                for row in rows:
                    legacy = json.loads(row[0])
                    legacy.update(
                        notification_id="legacy:" + legacy["id"],
                        notification_at=legacy.get("notification_revision", legacy["last_updated"]),
                        notification_price=None,
                        escalated_price=None,
                        price_provenance="LEGACY_UNKNOWN",
                    )
                    db.execute(
                        "INSERT INTO alert_notifications "
                        "(notification_id,alert_id,event_at,payload) VALUES (?,?,?,?)",
                        (
                            legacy["notification_id"],
                            legacy["id"],
                            legacy["notification_at"],
                            json.dumps(legacy, allow_nan=False),
                        ),
                    )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_raw(self, source: str, endpoint: str, payload: Any, at: datetime) -> str:
        raw_id = new_id()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        if len(encoded) > 4096:
            encoded = (
                "gzip:"
                + base64.b64encode(
                    gzip.compress(encoded.encode(), compresslevel=1, mtime=0)
                ).decode()
            )
        with self.connect() as db:
            db.execute(
                "INSERT INTO raw_observations VALUES (?,?,?,?,?,?)",
                (raw_id, source, endpoint, stamp(at), digest, encoded),
            )
        return raw_id

    def raw(self, raw_id: str, *, endpoint_paths: tuple[str, ...] = ()) -> dict | None:
        with self.connect() as db:
            if endpoint_paths:
                metadata = db.execute(
                    "SELECT endpoint FROM raw_observations WHERE id=?", (raw_id,)
                ).fetchone()
                if not metadata or urlsplit(metadata[0]).path not in endpoint_paths:
                    return None
            row = db.execute("SELECT * FROM raw_observations WHERE id=?", (raw_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        encoded = result["payload"]
        if encoded.startswith("gzip:"):
            encoded = gzip.decompress(base64.b64decode(encoded[5:])).decode()
        result["payload"] = json.loads(encoded)
        return result

    def save_snapshot(self, snapshot: Snapshot) -> None:
        if snapshot.market_time > snapshot.available_at:
            raise ValueError("Market data from the future must not be stored as available")
        if any(c.close_time > snapshot.available_at for c in snapshot.candles):
            raise ValueError("Snapshot must not include open or future candles")
        if snapshot.decision_at and snapshot.decision_at < snapshot.available_at:
            raise ValueError("Decision time cannot precede availability")
        with self.connect() as db:
            for raw_id in snapshot.raw_ids:
                row = db.execute(
                    "SELECT received_at FROM raw_observations WHERE id=?", (raw_id,)
                ).fetchone()
                if not row or datetime.fromisoformat(row[0]) > snapshot.available_at:
                    raise ValueError("Snapshot lineage is missing or was not yet available")
            db.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                (
                    snapshot.id,
                    snapshot.asset_id,
                    snapshot.module,
                    stamp(snapshot.market_time),
                    stamp(snapshot.available_at),
                    snapshot.model_dump_json(),
                ),
            )

    def snapshot(self, snapshot_id: str) -> Snapshot | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return Snapshot.model_validate_json(row[0]) if row else None

    def snapshots_by_ids(self, snapshot_ids: list[str]) -> dict[str, Snapshot]:
        result = {}
        ids = list(dict.fromkeys(snapshot_ids))
        with self.connect() as db:
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                rows = db.execute(
                    "SELECT payload FROM snapshots WHERE id IN ("
                    + ",".join("?" for _ in batch)
                    + ")",
                    batch,
                ).fetchall()
                for row in rows:
                    snapshot = Snapshot.model_validate_json(row[0])
                    result[snapshot.id] = snapshot
        return result

    def latest_snapshots(
        self, as_of: datetime, *, asset_ids: list[str] | None = None
    ) -> list[Snapshot]:
        if asset_ids == []:
            return []
        requested = "SELECT DISTINCT asset_id FROM snapshots"
        args: list = []
        if asset_ids is not None:
            ids = list(dict.fromkeys(asset_ids))
            requested = "VALUES " + ",".join("(?)" for _ in ids)
            args.extend(ids)
        args.append(stamp(as_of))
        with self.connect() as db:
            rows = db.execute(
                f"""
                WITH assets(asset_id) AS ({requested})
                SELECT s.payload FROM assets a JOIN snapshots s ON s.rowid=(
                    SELECT rowid FROM snapshots WHERE asset_id=a.asset_id AND available_at<=?
                    ORDER BY available_at DESC,rowid DESC LIMIT 1
                )
                ORDER BY s.asset_id
            """,
                args,
            ).fetchall()
        return [Snapshot.model_validate_json(row[0]) for row in rows]

    def snapshot_range(
        self, start: datetime, end: datetime, asset_id: str | None = None
    ) -> list[Snapshot]:
        query = "SELECT payload FROM snapshots WHERE available_at>=? AND available_at<=?"
        args: list = [stamp(start), stamp(end)]
        if asset_id:
            query += " AND asset_id=?"
            args.append(asset_id)
        query += " ORDER BY available_at, rowid"
        with self.connect() as db:
            rows = db.execute(query, args).fetchall()
        return [Snapshot.model_validate_json(row[0]) for row in rows]

    def save_signal(self, signal: Signal) -> None:
        for snapshot_id in [signal.snapshot_id, signal.btc_snapshot_id]:
            if snapshot_id:
                snapshot = self.snapshot(snapshot_id)
                if not snapshot or snapshot.available_at > signal.emitted_at:
                    raise ValueError("Signal cannot refer to missing or future evidence")
        event = SignalEvent(
            signal_id=signal.id,
            state="ACTIVE",
            event_at=signal.emitted_at,
            reason="规则触发，进入前瞻观察",
            snapshot_id=signal.snapshot_id,
        )
        with self.connect() as db:
            db.execute(
                "INSERT INTO signals VALUES (?,?,?,?,?,?)",
                (
                    signal.id,
                    signal.asset_id,
                    signal.rule_id,
                    signal.kind,
                    stamp(signal.emitted_at),
                    signal.model_dump_json(),
                ),
            )
            db.execute(
                "INSERT INTO signal_events VALUES (?,?,?,?,?)",
                (
                    event.id,
                    event.signal_id,
                    stamp(event.event_at),
                    event.state,
                    event.model_dump_json(),
                ),
            )

    def add_event(self, event: SignalEvent) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO signal_events VALUES (?,?,?,?,?)",
                (
                    event.id,
                    event.signal_id,
                    stamp(event.event_at),
                    event.state,
                    event.model_dump_json(),
                ),
            )

    def signals(self, limit: int = 200, as_of: datetime | None = None) -> list[Signal]:
        with self.connect() as db:
            if as_of:
                rows = db.execute(
                    "SELECT payload FROM signals WHERE emitted_at<=? "
                    "ORDER BY emitted_at DESC, rowid DESC LIMIT ?",
                    (stamp(as_of), limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT payload FROM signals ORDER BY emitted_at DESC, rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [Signal.model_validate_json(row[0]) for row in rows]

    def signal(self, signal_id: str) -> Signal | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM signals WHERE id=?", (signal_id,)).fetchone()
        return Signal.model_validate_json(row[0]) if row else None

    def signal_events(self, signal_id: str) -> list[SignalEvent]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM signal_events WHERE signal_id=? ORDER BY event_at, rowid",
                (signal_id,),
            ).fetchall()
        return [SignalEvent.model_validate_json(row[0]) for row in rows]

    def last_signal_time(
        self, asset_id: str, rule_id: str, kind: str, *, rule_version: str | None = None
    ) -> datetime | None:
        query = "SELECT MAX(emitted_at) FROM signals WHERE asset_id=? AND rule_id=? AND kind=?"
        args = [asset_id, rule_id, kind]
        if rule_version is not None:
            query += " AND json_extract(payload,'$.rule_version')=?"
            args.append(rule_version)
        with self.connect() as db:
            row = db.execute(query, args).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def save_outcome(self, outcome: Outcome) -> bool:
        with self.connect() as db:
            result = db.execute(
                "INSERT OR IGNORE INTO outcomes VALUES (?,?,?)",
                (outcome.signal_id, outcome.horizon_seconds, outcome.model_dump_json()),
            )
        return result.rowcount == 1

    def outcomes(self) -> list[Outcome]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM outcomes").fetchall()
        return [Outcome.model_validate_json(row[0]) for row in rows]

    def save_oi(self, asset_id: str, at: datetime, oi_usd: float) -> None:
        """累积衍生品持仓量读数（OKX 无公开 OI 历史接口，本地攒出 5 分钟变化）。"""
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO derivatives_oi VALUES (?,?,?)",
                (asset_id, stamp(at), oi_usd),
            )
            db.execute(
                "DELETE FROM derivatives_oi WHERE ts < ?",
                (stamp(at - timedelta(hours=6)),),
            )

    def oi_history(self, asset_id: str, since: datetime) -> list[tuple[datetime, float]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT ts, oi_usd FROM derivatives_oi WHERE asset_id=? AND ts>=? ORDER BY ts",
                (asset_id, stamp(since)),
            ).fetchall()
        return [(datetime.fromisoformat(row[0]), row[1]) for row in rows]

    def delete_status(self, name: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM provider_status WHERE name=?", (name,))

    def save_status(self, status: ProviderStatus) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO provider_status VALUES (?,?) "
                "ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                (status.name, status.model_dump_json()),
            )

    def statuses(self) -> list[ProviderStatus]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM provider_status ORDER BY name").fetchall()
        return [ProviderStatus.model_validate_json(row[0]) for row in rows]

    def save_regime(self, regime: Regime) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO regimes VALUES (?,?)",
                (stamp(regime.as_of), regime.model_dump_json()),
            )

    def counts(self) -> dict[str, int]:
        with self.connect() as db:
            return {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("raw_observations", "snapshots", "signals", "outcomes")
            }

    def set_state(self, key: str, payload: Any) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO runtime_state VALUES (?,?)",
                (key, json.dumps(payload, allow_nan=False)),
            )

    def state(self, key: str) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM runtime_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_universe(self, payload: dict) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO universe_history VALUES (?,?,?)",
                (new_id(), payload["selected_at"], json.dumps(payload, allow_nan=False)),
            )
            for entry in payload["entries"]:
                db.execute(
                    "INSERT OR REPLACE INTO instrument_registry VALUES (?,?)",
                    (entry["canonical_asset_id"], json.dumps(entry, allow_nan=False)),
                )

    def universe(self, as_of: datetime | None = None) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM universe_history WHERE selected_at<=? "
                "ORDER BY selected_at DESC,rowid DESC LIMIT 1",
                (stamp(as_of or datetime.now(UTC)),),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def rankings(self, as_of: datetime | None = None) -> list[dict]:
        with self.connect() as db:
            at = stamp(as_of or datetime.now(UTC))
            row = db.execute(
                "SELECT batch_id FROM ranking_history WHERE as_of<=? "
                "ORDER BY as_of DESC,rowid DESC LIMIT 1",
                (at,),
            ).fetchone()
            if not row:
                return []
            rows = db.execute(
                "SELECT payload FROM ranking_history WHERE batch_id=?", (row[0],)
            ).fetchall()
        return sorted((json.loads(r[0]) for r in rows), key=lambda r: r["rank"])

    def save_rankings(self, rankings: list[dict], at: datetime) -> None:
        batch_id = new_id()
        with self.connect() as db:
            for row in rankings:
                db.execute(
                    "INSERT INTO ranking_history VALUES (?,?,?,?)",
                    (
                        batch_id,
                        stamp(at),
                        row["canonical_asset_id"],
                        json.dumps(row, allow_nan=False),
                    ),
                )

    def alerts(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        level: str | None = None,
        since: datetime | None = None,
        unread: bool = False,
        pinned: bool = False,
        now: datetime | None = None,
    ) -> list[dict]:
        conditions, args = [], []
        if level:
            conditions.append("json_extract(payload,'$.level')=?")
            args.append(level)
        if since:
            conditions.append(
                "julianday(COALESCE(json_extract(payload,'$.notification_revision'),"
                "json_extract(payload,'$.first_seen')))>=julianday(?)"
            )
            args.append(stamp(since))
        if pinned:
            conditions.append("json_extract(payload,'$.pinned')=1")
        if unread:
            if now is None:
                raise ValueError("Unread filtering requires a current time")
            conditions.extend(
                [
                    "json_extract(payload,'$.read_at') IS NULL",
                    "json_extract(payload,'$.resolved_at') IS NULL",
                    "julianday(json_extract(payload,'$.expires_at'))>julianday(?)",
                ]
            )
            args.append(stamp(now))
        query = "SELECT payload FROM web_alerts"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY last_updated DESC,id"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            args.extend([limit, offset])
        with self.connect() as db:
            rows = db.execute(query, args).fetchall()
        return [json.loads(r[0]) for r in rows]

    def alerts_by_ids(self, alert_ids: list[str]) -> dict[str, dict]:
        result = {}
        ids = list(dict.fromkeys(alert_ids))
        with self.connect() as db:
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                rows = db.execute(
                    "SELECT id,payload FROM web_alerts WHERE id IN ("
                    + ",".join("?" for _ in batch)
                    + ")",
                    batch,
                ).fetchall()
                result.update({row[0]: json.loads(row[1]) for row in rows})
        return result

    def alert(self, alert_id: str) -> dict | None:
        return self.alerts_by_ids([alert_id]).get(alert_id)

    def active_alert(self, asset_id: str, rule_id: str, now: datetime) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM web_alerts WHERE asset_id=? AND rule_id=? "
                "AND json_extract(payload,'$.resolved_at') IS NULL "
                "AND julianday(json_extract(payload,'$.expires_at'))>julianday(?) "
                "ORDER BY last_updated DESC,id LIMIT 1",
                (asset_id, rule_id, stamp(now)),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def notification_page(
        self, after: int, limit: int, through: int | None = None, generation: str | None = None
    ) -> dict:
        with self.connect() as db:
            actual_generation = json.loads(
                db.execute(
                    "SELECT payload FROM runtime_state WHERE key='alert_notification_feed'"
                ).fetchone()[0]
            )["generation"]
            if generation is not None and generation != actual_generation:
                raise ValueError("通知库已更换，请从 after=0 重新读取，并按 notification_id 去重")
            if after and generation is None:
                raise ValueError("续传需要上次返回的 generation")
            upper = db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM alert_notifications"
            ).fetchone()[0]
            boundary = upper if through is None else through
            if after < 0 or limit < 1 or after > boundary or boundary > upper:
                raise ValueError("游标超出通知库范围，请核查数据库恢复情况")
            rows = db.execute(
                "SELECT sequence,payload FROM alert_notifications "
                "WHERE sequence>? AND sequence<=? ORDER BY sequence LIMIT ?",
                (after, boundary, limit + 1),
            ).fetchall()
        items = [dict(json.loads(row[1]), sequence=row[0]) for row in rows[:limit]]
        return {
            "generation": actual_generation,
            "items": items,
            "next_cursor": items[-1]["sequence"] if items else after,
            "upper_cursor": boundary,
            "has_more": len(rows) > limit,
        }

    def alert_events(self, alert_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM web_alert_events WHERE alert_id=? ORDER BY event_at,rowid",
                (alert_id,),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_strategy_run(self, summary, rows):
        with self.connect() as db:
            db.execute(
                "INSERT INTO strategy_runs VALUES (?,?,?)",
                (summary["batch_id"], summary["as_of"], json.dumps(summary, allow_nan=False)),
            )
            db.executemany(
                "INSERT OR REPLACE INTO strategy_latest VALUES (?,?,?)",
                [(r["asset_id"], r["rule_id"], json.dumps(r, allow_nan=False)) for r in rows],
            )

    def strategy_report(self, asset_id=None, limit=100):
        with self.connect() as db:
            run = db.execute(
                "SELECT payload FROM strategy_runs ORDER BY as_of DESC,rowid DESC LIMIT 1"
            ).fetchone()
            query, args = "SELECT payload FROM strategy_latest", []
            if asset_id:
                query += " WHERE asset_id=?"
                args.append(asset_id)
            rows = db.execute(
                query + " ORDER BY asset_id,rule_id LIMIT ?", args + [limit]
            ).fetchall()
        return json.loads(run[0]) if run else None, [json.loads(r[0]) for r in rows]

    def notification_receipts(self, ids):
        result = {}
        with self.connect() as db:
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                rows = db.execute(
                    "SELECT notification_id,payload FROM notification_receipts "
                    "WHERE notification_id IN (" + ",".join("?" for _ in batch) + ")",
                    batch,
                ).fetchall()
                result.update({r[0]: json.loads(r[1]) for r in rows})
        return result

    def notification_origins(self, signal_ids, now):
        """Published STRONG revisions and their self-reported delivery, bounded by IDs/time."""
        result = {}
        ids = list(dict.fromkeys(signal_ids))
        with self.connect() as db:
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                rows = db.execute(
                    "SELECT n.payload,r.payload FROM alert_notifications n "
                    "LEFT JOIN notification_receipts r ON r.notification_id=n.notification_id "
                    "WHERE json_extract(n.payload,'$.signal_id') IN ("
                    + ",".join("?" for _ in batch)
                    + ") AND n.event_at<=? "
                    "AND json_extract(n.payload,'$.level')='STRONG' ORDER BY n.sequence",
                    [*batch, stamp(now)],
                ).fetchall()
                for payload, receipt in rows:
                    item = json.loads(payload)
                    item["receipt"] = json.loads(receipt) if receipt else {}
                    if (
                        item["receipt"]
                        and datetime.fromisoformat(item["receipt"]["received_at"]) > now
                    ):
                        item["receipt"] = {}
                    result.setdefault(item["signal_id"], []).append(item)
        return result

    def signals_by_ids(self, ids):
        result = {}
        with self.connect() as db:
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                rows = db.execute(
                    "SELECT id,payload FROM signals WHERE id IN ("
                    + ",".join("?" for _ in batch)
                    + ")",
                    batch,
                ).fetchall()
                result.update({r[0]: Signal.model_validate_json(r[1]) for r in rows})
        return result

    def save_notification_receipt(self, ids, status, message_id, reason, now):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Validate the complete batch before changing any receipt.
            for notification_id in ids:
                if not db.execute(
                    "SELECT 1 FROM alert_notifications WHERE notification_id=?", (notification_id,)
                ).fetchone():
                    raise ValueError("通知不存在: " + notification_id)
            for notification_id in ids:
                prior = db.execute(
                    "SELECT payload FROM notification_receipts WHERE notification_id=?",
                    (notification_id,),
                ).fetchone()
                if prior:
                    prior_status = json.loads(prior[0])["status"]
                    if prior_status == "SENT" or prior_status == "SKIPPED" and status != "SENT":
                        continue
                payload = {
                    "status": status,
                    "message_id": message_id,
                    "reason": reason,
                    "received_at": now.isoformat(),
                    "source": "MUSE_REPORTED",
                }
                db.execute(
                    "INSERT OR REPLACE INTO notification_receipts VALUES (?,?)",
                    (notification_id, json.dumps(payload)),
                )

    def save_alert(self, alert: dict, event: dict | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO web_alerts VALUES (?,?,?,?,?)",
                (
                    alert["id"],
                    alert["asset_id"],
                    alert.get("storage_rule_id", alert["rule_id"]),
                    alert["last_updated"],
                    json.dumps(alert, allow_nan=False),
                ),
            )
            if event:
                db.execute(
                    "INSERT INTO web_alert_events VALUES (?,?,?,?)",
                    (
                        event.get("notification_id", new_id()),
                        alert["id"],
                        event["event_at"],
                        json.dumps(event, allow_nan=False),
                    ),
                )
                if event.get("notification_id") and (
                    alert["level"] in ("STRONG", "CRITICAL_RISK")
                    or alert.get("notification_class") == "CANCELLATION"
                    and alert.get("parent_had_strong_notice")
                ):
                    db.execute(
                        "INSERT INTO alert_notifications "
                        "(notification_id,alert_id,event_at,payload) VALUES (?,?,?,?)",
                        (
                            event["notification_id"],
                            alert["id"],
                            event["event_at"],
                            json.dumps(alert, allow_nan=False),
                        ),
                    )

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)

    def save_decision_config(self, at, settings):
        from .decisions import decision_config

        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO decision_configs VALUES (?,?)",
                (stamp(at), json.dumps(decision_config(settings))),
            )

    def decision_config(self, at):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM decision_configs WHERE as_of=?", (stamp(at),)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def diagnostics(self):
        with self.connect() as db:
            counts = {
                name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                for name in (
                    "snapshots",
                    "raw_observations",
                    "ranking_history",
                    "intelligence_records",
                    "evidence_checks",
                )
            }
            try:
                table_bytes = {
                    row[0]: row[1]
                    for row in db.execute("SELECT name,SUM(pgsize) FROM dbstat GROUP BY name")
                }
            except sqlite3.OperationalError:
                table_bytes = None  # Some SQLite builds omit the dbstat virtual table.
        return {
            "rows": counts,
            "table_and_index_bytes": table_bytes,
            "database_bytes": self.path.stat().st_size,
            "wal_bytes": Path(str(self.path) + "-wal").stat().st_size
            if Path(str(self.path) + "-wal").exists()
            else 0,
            "retention": "PRESERVE_LINEAGE",
            "schema_version": 4,
        }
