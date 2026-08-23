"""Content recommendation scoring.

Step 1 of the recommendation engine (see GitHub issue #226): "similar content"
recommendations based on shared reciter/riwayah/qiraah/category facets.

Design notes:
- Candidate generation is facet-indexed, not O(n^2): for each asset we only ever compare
  against the (typically small) sets of other assets sharing the same reciter, riwayah,
  qiraah, or category, rather than scanning the whole catalog per asset.
- Riwayah/qiraah don't double-count: a qiraah-match point is only awarded when the
  riwayah doesn't already match, since (per Asset.save()) riwayah implies its qiraah.
- Only READY assets that are visible on public surfaces (restricted_for_tenant=False)
  are considered as candidates or sources, so recommendations never surface or point at
  content that shouldn't appear on the public developers API.
- Results are written to Redis as sorted sets (see recommendations_redis.similar_key) by
  the nightly Celery task; get_similar_asset_ids only reads, it never computes inline.
"""

from __future__ import annotations

from collections import defaultdict
import logging

from apps.content.models import Asset, CategoryChoice, StatusChoice
from apps.content.services.recommendations_redis import get_recommendations_redis, similar_key

logger = logging.getLogger(__name__)

# How many similar-asset ids to keep per source asset.
SIMILAR_TOP_N = 10

# Score weights. Same reciter + riwayah is the strongest signal (same person, same
# recitation style); category is the weakest (broad content-type match only).
_WEIGHT_RECITER = 3
_WEIGHT_RIWAYAH = 2
_WEIGHT_QIRAAH = 1  # only applied when riwayah doesn't already match
_WEIGHT_CATEGORY = 1

# TTL on similar:* keys: nightly recompute refreshes them well within this window, but a
# TTL keeps a paused/broken beat schedule from serving arbitrarily stale data forever.
SIMILAR_KEY_TTL_SECONDS = 60 * 60 * 48  # 48 hours


def _visible_asset_values() -> list[dict]:
    """READY, publicly-visible assets, as plain dicts for cheap in-memory scoring."""
    return list(
        Asset.objects.filter(
            status=StatusChoice.READY,
            restricted_for_tenant=False,
        ).values("id", "category", "reciter_id", "riwayah_id", "qiraah_id")
    )


def _score_pairs(assets: list[dict]) -> dict[int, dict[int, int]]:
    """Return {asset_id: {other_asset_id: score}} using facet-indexed candidate sets."""
    by_reciter: dict[int, set[int]] = defaultdict(set)
    by_riwayah: dict[int, set[int]] = defaultdict(set)
    by_qiraah: dict[int, set[int]] = defaultdict(set)
    by_category: dict[str, set[int]] = defaultdict(set)
    asset_by_id: dict[int, dict] = {}

    for a in assets:
        asset_by_id[a["id"]] = a
        if a["reciter_id"]:
            by_reciter[a["reciter_id"]].add(a["id"])
        if a["riwayah_id"]:
            by_riwayah[a["riwayah_id"]].add(a["id"])
        if a["qiraah_id"]:
            by_qiraah[a["qiraah_id"]].add(a["id"])
        by_category[a["category"]].add(a["id"])

    scores: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))

    for a in assets:
        asset_id = a["id"]
        candidates: set[int] = set()
        if a["reciter_id"]:
            candidates |= by_reciter[a["reciter_id"]]
        if a["riwayah_id"]:
            candidates |= by_riwayah[a["riwayah_id"]]
        if a["qiraah_id"]:
            candidates |= by_qiraah[a["qiraah_id"]]
        candidates |= by_category[a["category"]]
        candidates.discard(asset_id)

        for other_id in candidates:
            other = asset_by_id[other_id]
            score = 0
            riwayah_match = bool(a["riwayah_id"]) and a["riwayah_id"] == other["riwayah_id"]

            if a["reciter_id"] and a["reciter_id"] == other["reciter_id"]:
                score += _WEIGHT_RECITER
            if riwayah_match:
                score += _WEIGHT_RIWAYAH
            elif a["qiraah_id"] and a["qiraah_id"] == other["qiraah_id"]:
                score += _WEIGHT_QIRAAH
            if a["category"] == other["category"]:
                score += _WEIGHT_CATEGORY

            if score > 0:
                scores[asset_id][other_id] = score

    return scores


def compute_similar_recommendations() -> dict:
    """Recompute similar-asset scores for every READY, public asset and write to Redis.

    Intended to run nightly via Celery beat (compute_similar_recommendations_task).
    Assets with no scored candidates get their key deleted rather than left stale from
    a previous run, so a since-removed/rescoped asset doesn't keep surfacing old
    recommendations.

    Returns a small summary dict for logging/observability.
    """
    redis_client = get_recommendations_redis()
    if redis_client is None:
        logger.warning("compute_similar_recommendations: no Redis available, skipping")
        return {"assets_scored": 0, "keys_written": 0}

    assets = _visible_asset_values()
    scores = _score_pairs(assets)

    keys_written = 0
    with redis_client.pipeline() as pipe:
        for a in assets:
            asset_id = a["id"]
            key = similar_key(asset_id)
            pipe.delete(key)
            asset_scores = scores.get(asset_id, {})
            if not asset_scores:
                continue
            top = sorted(asset_scores.items(), key=lambda kv: kv[1], reverse=True)[:SIMILAR_TOP_N]
            pipe.zadd(key, {str(other_id): score for other_id, score in top})
            pipe.expire(key, SIMILAR_KEY_TTL_SECONDS)
            keys_written += 1
        pipe.execute()

    logger.info(f"compute_similar_recommendations: scored {len(assets)} assets, wrote {keys_written} keys")
    return {"assets_scored": len(assets), "keys_written": keys_written}


def get_similar_asset_ids(asset_id: int, limit: int = SIMILAR_TOP_N) -> list[int]:
    """Read precomputed similar-asset ids for `asset_id`, most similar first.

    Returns an empty list when Redis is unavailable or no precomputed entry exists
    (e.g. asset created after the last nightly run, or genuinely has no matches) —
    callers should treat that as "no recommendations yet", not an error.
    """
    redis_client = get_recommendations_redis()
    if redis_client is None:
        return []

    raw_ids = redis_client.zrevrange(similar_key(asset_id), 0, limit - 1)
    return [int(raw_id) for raw_id in raw_ids]


def hydrate_visible_assets_in_order(asset_ids: list[int]) -> list[Asset]:
    """Fetch Assets for `asset_ids`, filtered to still-visible ones, preserving order.

    Precomputed ids can go stale between the nightly run and the request (asset
    unpublished, restricted, or deleted since) — those are silently dropped rather than
    erroring, matching how the recitation-tracks endpoint treats cache/DB drift.
    """
    if not asset_ids:
        return []

    qs = Asset.objects.filter(
        id__in=asset_ids,
        status=StatusChoice.READY,
        restricted_for_tenant=False,
    ).select_related("publisher", "reciter", "riwayah", "qiraah")

    by_id = {a.id: a for a in qs}
    return [by_id[aid] for aid in asset_ids if aid in by_id]


__all__ = [
    "CategoryChoice",
    "SIMILAR_TOP_N",
    "compute_similar_recommendations",
    "get_similar_asset_ids",
    "hydrate_visible_assets_in_order",
]
