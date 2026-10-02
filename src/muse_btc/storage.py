import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
                CREATE TABLE IF NOT EXISTS regimes (
                    as_of TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

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
        with self.connect() as db:
            db.execute(
                "INSERT INTO raw_observations VALUES (?,?,?,?,?,?)",
                (raw_id, source, endpoint, stamp(at), digest, encoded),
            )
        return raw_id

    def raw(self, raw_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM raw_observations WHERE id=?", (raw_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
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

    def latest_snapshots(self, as_of: datetime) -> list[Snapshot]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT payload FROM (
                    SELECT payload, ROW_NUMBER() OVER (
                        PARTITION BY asset_id ORDER BY available_at DESC, rowid DESC
                    ) AS rank FROM snapshots WHERE available_at<=?
                ) WHERE rank=1
            """,
                (stamp(as_of),),
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

    def last_signal_time(self, asset_id: str, rule_id: str, kind: str) -> datetime | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT MAX(emitted_at) FROM signals WHERE asset_id=? AND rule_id=? AND kind=?",
                (asset_id, rule_id, kind),
            ).fetchone()
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
