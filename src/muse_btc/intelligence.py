"""Append-only, point-in-time evidence shared by research and optional intelligence modules."""

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import AwareDatetime, ConfigDict, Field

from .models import Record, new_id
from .storage import Store, stamp

KINDS = {
    "research",
    "research_review",
    "macro",
    "macro_event",
    "macro_calendar",
    "btc_event_quote",
    "stablecoin",
    "etf",
    "catalyst",
    "tokenomics",
    "fundamental",
    "meme",
    "holder",
    "wallet_trade",
    "wallet_link",
    "social",
    "social_thesis",
    "model",
    "paid_evaluation",
    "onchain",
    "options",
    "liquidation",
    "liquidation_window",
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def canonical_url(value: str) -> str:
    u = urlsplit(value)
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        raise ValueError("A public HTTPS source URL is required")
    return urlunsplit((u.scheme, u.netloc.lower(), u.path.rstrip("/"), "", ""))


class EvidenceRecord(Record):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(default_factory=new_id)
    kind: str
    key: str = Field(min_length=1, max_length=300)
    source: str = Field(min_length=1, max_length=200)
    market_time: AwareDatetime
    available_at: AwareDatetime
    raw_ids: list[str] = []
    data: dict[str, Any]
    version: str = "intelligence-v4-2"


class IntelligenceStore:
    def __init__(self, store: Store):
        self.store = store

    def save(self, record: EvidenceRecord) -> EvidenceRecord:
        if record.kind not in KINDS or record.market_time > record.available_at:
            raise ValueError("Unknown evidence type or future source time")
        content_hash = digest(record.data)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for raw_id in record.raw_ids:
                raw = db.execute(
                    "SELECT received_at FROM raw_observations WHERE id=?", (raw_id,)
                ).fetchone()
                if not raw or datetime.fromisoformat(raw[0]) > record.available_at:
                    raise ValueError("Evidence refers to missing or future raw data")
            old = db.execute(
                "SELECT payload,content_hash FROM intelligence_records WHERE kind=? AND key=? "
                "ORDER BY available_at DESC,rowid DESC LIMIT 1",
                (record.kind, record.key),
            ).fetchone()
            if old and old[1] == content_hash:
                saved = EvidenceRecord.model_validate_json(old[0])
                db.execute(
                    "INSERT OR IGNORE INTO evidence_checks VALUES (?,?,?)",
                    (saved.id, stamp(record.available_at), json.dumps(record.raw_ids)),
                )
                return saved
            db.execute(
                "INSERT INTO intelligence_records VALUES (?,?,?,?,?,?,?)",
                (
                    record.id,
                    record.kind,
                    record.key,
                    stamp(record.market_time),
                    stamp(record.available_at),
                    content_hash,
                    record.model_dump_json(),
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO evidence_checks VALUES (?,?,?)",
                (record.id, stamp(record.available_at), json.dumps(record.raw_ids)),
            )
        return record

    def last_checked(self, record, as_of):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT MAX(checked_at) FROM evidence_checks WHERE record_id=? AND checked_at<=?",
                (record.id, stamp(as_of)),
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else record.available_at

    def renew(self, name, token, now, seconds=120):
        with self.store.connect() as db:
            changed = db.execute(
                "UPDATE collection_leases SET expires_at=? "
                "WHERE name=? AND token=? AND expires_at>?",
                (stamp(now + timedelta(seconds=seconds)), name, token, stamp(now)),
            )
        return bool(changed.rowcount)

    def records(
        self,
        kind: str,
        as_of: datetime,
        key: str | None = None,
        limit: int = 1000,
        latest: bool = True,
    ) -> list[EvidenceRecord]:
        where = "kind=? AND available_at<=?"
        args: list = [kind, stamp(as_of)]
        if key:
            where += " AND key=?"
            args.append(key)
        limit = max(1, min(limit, 100000))
        if latest:
            query = (
                "SELECT evidence.payload FROM (SELECT available_at,rowid AS sequence, "
                "ROW_NUMBER() OVER (PARTITION BY key ORDER BY available_at DESC,rowid DESC) AS n "
                "FROM intelligence_records WHERE "
                + where
                + ") latest JOIN intelligence_records evidence "
                "ON evidence.rowid=latest.sequence WHERE latest.n=1 "
                "ORDER BY latest.available_at DESC,latest.sequence DESC LIMIT ?"
            )
        else:
            query = (
                "SELECT payload FROM intelligence_records WHERE "
                + where
                + " ORDER BY available_at DESC,rowid DESC LIMIT ?"
            )
        with self.store.connect() as db:
            rows = db.execute(query, [*args, limit]).fetchall()
        return [EvidenceRecord.model_validate_json(row[0]) for row in rows]

    def by_id(self, record_id: str, as_of: datetime) -> EvidenceRecord | None:
        with self.store.connect() as db:
            row = db.execute(
                "SELECT payload FROM intelligence_records WHERE id=? AND available_at<=?",
                (record_id, stamp(as_of)),
            ).fetchone()
        return EvidenceRecord.model_validate_json(row[0]) if row else None

    def checkpoint(self, source: str) -> datetime | None:
        with self.store.connect() as db:
            row = db.execute(
                "SELECT MAX(checked_at) FROM research_checks WHERE source=? AND succeeded=1",
                (source,),
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def checked(self, source: str, now: datetime, success: bool, message: str) -> None:
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO research_checks VALUES (?,?,?,?,?)",
                (new_id(), source, stamp(now), int(success), message),
            )

    def checks(self) -> list[dict]:
        with self.store.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT source,checked_at,succeeded,message "
                    "FROM research_checks ORDER BY checked_at DESC LIMIT 100"
                )
            ]

    def acquire(self, name: str, now: datetime, seconds: int = 300) -> str | None:
        token = new_id()
        with self.store.connect() as db:
            db.execute("DELETE FROM collection_leases WHERE expires_at<=?", (stamp(now),))
            changed = db.execute(
                "INSERT OR IGNORE INTO collection_leases VALUES (?,?,?)",
                (name, token, stamp(now + timedelta(seconds=seconds))),
            )
        return token if changed.rowcount else None

    def release(self, name: str, token: str) -> None:
        with self.store.connect() as db:
            db.execute("DELETE FROM collection_leases WHERE name=? AND token=?", (name, token))


class ContextInput(Record):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    kind: Literal[
        "catalyst",
        "tokenomics",
        "fundamental",
        "macro_event",
        "etf",
        "holder",
        "wallet_trade",
        "wallet_link",
        "social",
        "social_thesis",
    ]
    key: str = Field(min_length=1, max_length=300)
    source_url: str
    market_time: AwareDatetime
    data: dict[str, Any]
