"""Social and narrative metrics over authenticated API responses or explicit imports."""

import re
import statistics
from collections import Counter
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import httpx

from .async_io import run_sync
from .context import import_context
from .intelligence import ContextInput, IntelligenceStore
from .models import utc_now

NARRATIVES = {
    "AI": ("ai", "agent", "artificial intelligence", "人工智能"),
    "动物": ("dog", "cat", "pepe", "frog", "狗", "猫"),
    "政治": ("election", "president", "trump", "政治"),
    "名人": ("celebrity", "musk", "名人"),
    "技术": ("solver", "intent", "rollup", "zk", "技术"),
    "文化": ("culture", "meme", "文化"),
    "突发热点": ("breaking", "viral", "热点"),
}


def normalized_text(text):
    return " ".join(re.sub(r"https?://\S+|@\w+|[^\w\s]", " ", text.lower()).split())


def narratives(text):
    text = text.lower()
    return [
        name
        for name, words in NARRATIVES.items()
        if any(
            re.search(r"\b" + re.escape(w) + r"\b", text) if w.isascii() else w in text
            for w in words
        )
    ] or ["其他"]


class SocialEngine:
    def __init__(self, store, settings, providers):
        self.store, self.settings, self.providers = store, settings, providers
        self.archive = IntelligenceStore(store)

    async def collect(self):
        if not self.settings.enable_social or not self.settings.x_bearer_token:
            return {"status": "NEEDS_CREDENTIALS", "posts": 0}
        token = await run_sync(self.archive.acquire, "social", utc_now(), 300)
        if not token:
            return {"status": "BUSY"}
        count = 0
        try:
            now = utc_now()
            checkpoint = await run_sync(self.store.state, "social_checkpoint") or {}
            if checkpoint.get("query") != self.settings.x_query:
                checkpoint = {}
            window = checkpoint.get("pending") or {
                "start_time": checkpoint.get("complete_through")
                or (now - timedelta(hours=2)).isoformat(),
                "end_time": (now - timedelta(seconds=30)).isoformat(),
            }
            if datetime.fromisoformat(window["start_time"]) >= datetime.fromisoformat(
                window["end_time"]
            ):
                return {"status": "COMPLETE", "posts": 0, "scope": "NO_NEW_WINDOW"}
            await run_sync(
                self.store.set_state,
                "social_checkpoint",
                {**checkpoint, "query": self.settings.x_query, "pending": window},
            )
            for _ in range(self.settings.social_max_pages):
                params = {
                    "query": self.settings.x_query,
                    "max_results": 100,
                    "tweet.fields": "created_at,public_metrics,author_id",
                    "expansions": "author_id",
                    "user.fields": "public_metrics",
                    **window,
                }
                response = await self.providers.client.get(
                    "https://api.x.com/2/tweets/search/recent",
                    params=params,
                    headers={
                        "Authorization": "Bearer " + self.settings.x_bearer_token.get_secret_value()
                    },
                )
                if not response.is_success:
                    return {
                        "status": "UNAVAILABLE",
                        "http_status": response.status_code,
                        "posts": count,
                    }
                payload = response.json()
                if not isinstance(payload, dict) or "meta" not in payload or payload.get("errors"):
                    return {
                        "status": "UNAVAILABLE",
                        "posts": count,
                        "reason": "Incomplete X response",
                    }
                at = utc_now()
                raw = await run_sync(
                    self.store.save_raw,
                    "X",
                    "https://api.x.com/2/tweets/search/recent",
                    payload,
                    at,
                )
                users = {u["id"]: u for u in payload.get("includes", {}).get("users", [])}
                for post in payload.get("data", []):
                    metrics = post.get("public_metrics", {})
                    user = users.get(post["author_id"], {})
                    item = ContextInput(
                        kind="social",
                        key=post["id"],
                        source_url="https://x.com/i/status/" + post["id"],
                        market_time=post["created_at"],
                        data={
                            "platform": "X",
                            "post_id": post["id"],
                            "author_id": post["author_id"],
                            "text": post["text"],
                            "likes": metrics.get("like_count", 0),
                            "replies": metrics.get("reply_count", 0),
                            "reposts": metrics.get("retweet_count", 0),
                            "followers": user.get("public_metrics", {}).get("followers_count"),
                        },
                    )
                    await run_sync(import_context, self.store, item, at)
                    count += 1
                next_token = payload.get("meta", {}).get("next_token")
                if not next_token:
                    await run_sync(
                        self.store.set_state,
                        "social_checkpoint",
                        {
                            "query": self.settings.x_query,
                            "complete_through": window["end_time"],
                            "last_raw_id": raw,
                            "pending": None,
                        },
                    )
                    return {
                        "status": "COMPLETE",
                        "posts": count,
                        "scope": "COMPLETE_QUERY_WINDOW",
                        "start_time": window["start_time"],
                        "end_time": window["end_time"],
                    }
                window["next_token"] = next_token
                await run_sync(
                    self.store.set_state,
                    "social_checkpoint",
                    {
                        **checkpoint,
                        "query": self.settings.x_query,
                        "pending": window,
                        "last_raw_id": raw,
                    },
                )
            return {
                "status": "DEGRADED",
                "posts": count,
                "scope": "PARTIAL_QUERY_WINDOW",
                "resume_pending": True,
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return {"status": "UNAVAILABLE", "posts": count}
        finally:
            await run_sync(self.archive.release, "social", token)

    def summary(self, now):
        posts = self.archive.records("social", now, limit=100000)
        recent = [r for r in posts if now - timedelta(hours=1) <= r.market_time <= now]
        previous = [
            r for r in posts if now - timedelta(hours=2) <= r.market_time < now - timedelta(hours=1)
        ]
        texts = [normalized_text(r.data["text"]) for r in recent]
        duplicates = sum(
            any(SequenceMatcher(None, text, earlier).ratio() >= 0.9 for earlier in texts[:i])
            for i, text in enumerate(texts)
        )
        authors = Counter(r.data["author_id"] for r in recent)
        engagement = [r.data["likes"] + r.data["replies"] + r.data["reposts"] for r in recent]
        narrative_rows = []
        for name in NARRATIVES.keys() | {"其他"}:
            current = [r for r in recent if name in narratives(r.data["text"])]
            earlier = [r for r in previous if name in narratives(r.data["text"])]
            if not current:
                continue
            assets = Counter(a for r in current for a in r.data["assets"])
            narrative_rows.append(
                {
                    "narrative": name,
                    "mentions_1h": len(current),
                    "velocity_per_hour": len(current),
                    "acceleration": len(current) - len(earlier),
                    "unique_authors": len({r.data["author_id"] for r in current}),
                    "novelty": not bool(earlier),
                    "token_count": len(assets),
                    "leader": assets.most_common(1)[0][0] if assets else None,
                    "saturation_proxy": len(current) / max(1, len(recent)),
                }
            )
        return {
            "as_of": now.isoformat(),
            "collection_window": self.store.state("social_checkpoint"),
            "change_interpretation": "OBSERVED_SAMPLE_ONLY_NOT_MARKET_WIDE",
            "observed_posts_1h": len(recent),
            "unique_authors": len(authors),
            "mention_velocity_per_hour": len(recent),
            "mention_acceleration": len(recent) - len(previous),
            "text_similarity_repeat_ratio": duplicates / len(recent) if recent else None,
            "paid_promotion_count": sum(r.data["paid_promotion"] is True for r in recent),
            "promotion_status_unknown_count": sum(r.data["paid_promotion"] is None for r in recent),
            "median_engagement": statistics.median(engagement) if engagement else None,
            "author_concentration": max(authors.values()) / len(recent) if recent else None,
            "organic_growth_proxy": (len(authors) / len(recent)) * (1 - duplicates / len(recent))
            if recent
            else None,
            "bot_probability": None,
            "narratives": narrative_rows,
            "kol_reputation": self.reputation(now),
            "limitations": [
                "API 与导入样本不是全站统计",
                "互动数不证明自然增长",
                "Bot 概率没有校准数据",
                "时间序列不充分时变化受采集覆盖影响",
                "叙事词典是规则分类，不是 LLM 理解",
            ],
        }

    def reputation(self, now):
        groups = {}
        for r in self.archive.records("social_thesis", now):
            d = r.data
            target = r.market_time + timedelta(seconds=d["horizon_seconds"])
            if target > now:
                continue
            end = next(
                (
                    s
                    for s in self.store.snapshot_range(
                        target, min(now, target + timedelta(seconds=240)), d["asset_id"]
                    )
                    if s.market_time >= target
                ),
                None,
            )
            if not end:
                continue
            value = (
                (end.price / d["reference_price"] - 1)
                * 100
                * (1 if d["direction"] == "BULLISH" else -1)
            )
            groups.setdefault(d["author_id"], []).append(value)
        return [
            {
                "author_id": author,
                "measured_theses": len(values),
                "direction_hit_rate": statistics.mean(v > 0 for v in values),
                "median_directional_return_pct": statistics.median(values),
                "reputation_status": "RESEARCH_ONLY",
                "calibrated_score": None,
            }
            for author, values in groups.items()
        ]
