from unittest.mock import patch

from model_bakery import baker
import redis as redis_lib

from apps.content.models import Asset, CategoryChoice, StatusChoice
from apps.content.services.recommendations import (
    compute_similar_recommendations,
    get_similar_asset_ids,
    hydrate_visible_assets_in_order,
)
from apps.content.services.recommendations_redis import similar_key
from apps.core.tests.base import BaseTestCase

# Dev settings run the Django cache on LocMemCache (see config/settings/development.py),
# so get_recommendations_redis() would resolve to None there -- exactly like
# apps.usage_tracking.tasks._get_tracking_redis. usage_tracking's tests mock that
# resolver with a MagicMock; here we point it at a real Redis test DB instead, so the
# scoring assertions below exercise genuine ZADD/ZRANGE/ZSCORE semantics rather than a
# mock's recorded calls. DB 15 is reserved for tests and flushed before/after each test.
_GET_REDIS = "apps.content.services.recommendations.get_recommendations_redis"


def _test_redis_client() -> redis_lib.Redis:
    return redis_lib.Redis(host="localhost", port=6379, db=15, decode_responses=True)


class RecommendationsServiceTest(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.redis_client = _test_redis_client()
        self.redis_client.flushdb()
        self._redis_patcher = patch(_GET_REDIS, return_value=self.redis_client)
        self._redis_patcher.start()

        self.qiraah = baker.make("content.Qiraah", name="Hafs")
        self.riwayah_a = baker.make("content.Riwayah", name="Riwayah A", qiraah=self.qiraah)
        self.riwayah_b = baker.make("content.Riwayah", name="Riwayah B", qiraah=self.qiraah)
        self.reciter_1 = baker.make("content.Reciter", name="Reciter 1")
        self.reciter_2 = baker.make("content.Reciter", name="Reciter 2")

    def tearDown(self):
        self.redis_client.flushdb()
        self._redis_patcher.stop()
        super().tearDown()

    def _make_recitation(self, **kwargs):
        defaults = {
            "category": CategoryChoice.RECITATION,
            "status": StatusChoice.READY,
            "restricted_for_tenant": False,
            "qiraah": self.qiraah,
        }
        defaults.update(kwargs)
        return baker.make(Asset, **defaults)

    def test_same_reciter_and_riwayah_scores_higher_than_same_riwayah_alone(self):
        # Arrange: source shares reciter+riwayah with `close`, only riwayah with `far`.
        source = self._make_recitation(reciter=self.reciter_1, riwayah=self.riwayah_a)
        close = self._make_recitation(reciter=self.reciter_1, riwayah=self.riwayah_a)
        far = self._make_recitation(reciter=self.reciter_2, riwayah=self.riwayah_a)

        # Act
        compute_similar_recommendations()
        result = get_similar_asset_ids(source.id)

        # Assert: both appear, but the stronger (reciter+riwayah) match ranks first.
        self.assertEqual([close.id, far.id], result)

    def test_qiraah_match_not_double_counted_when_riwayah_already_matches(self):
        """riwayah implies its qiraah (Asset.save()), so a riwayah match alone should
        score the same as a riwayah+qiraah match -- not double the qiraah weight."""
        source = self._make_recitation(reciter=self.reciter_1, riwayah=self.riwayah_a)
        same_riwayah = self._make_recitation(reciter=self.reciter_2, riwayah=self.riwayah_a)
        same_qiraah_only = self._make_recitation(reciter=self.reciter_2, riwayah=self.riwayah_b)

        compute_similar_recommendations()

        same_riwayah_score = self.redis_client.zscore(similar_key(source.id), str(same_riwayah.id))
        same_qiraah_score = self.redis_client.zscore(similar_key(source.id), str(same_qiraah_only.id))

        # riwayah match (weight 2) beats a bare qiraah-only match (weight 1).
        self.assertGreater(same_riwayah_score, same_qiraah_score)

    def test_unrelated_category_asset_is_not_a_candidate(self):
        source = self._make_recitation(reciter=self.reciter_1, riwayah=self.riwayah_a)
        unrelated = baker.make(
            Asset,
            category=CategoryChoice.TAFSIR,
            status=StatusChoice.READY,
            restricted_for_tenant=False,
            reciter=None,
            riwayah=None,
            qiraah=None,
        )

        compute_similar_recommendations()
        result = get_similar_asset_ids(source.id)

        self.assertNotIn(unrelated.id, result)

    def test_draft_and_restricted_assets_excluded_from_scoring(self):
        source = self._make_recitation(reciter=self.reciter_1, riwayah=self.riwayah_a)
        draft = self._make_recitation(reciter=self.reciter_1, riwayah=self.riwayah_a, status=StatusChoice.DRAFT)
        restricted = self._make_recitation(reciter=self.reciter_1, riwayah=self.riwayah_a, restricted_for_tenant=True)

        compute_similar_recommendations()
        result = get_similar_asset_ids(source.id)

        self.assertNotIn(draft.id, result)
        self.assertNotIn(restricted.id, result)

    def test_recompute_clears_stale_entries(self):
        """An asset that used to have a match but no longer does (e.g. sibling asset
        deleted) should have its Redis key cleared, not left with stale ids."""
        source = self._make_recitation(reciter=self.reciter_1, riwayah=self.riwayah_a)
        sibling = self._make_recitation(reciter=self.reciter_1, riwayah=self.riwayah_a)

        compute_similar_recommendations()
        self.assertEqual([sibling.id], get_similar_asset_ids(source.id))

        sibling.delete()
        compute_similar_recommendations()

        self.assertEqual([], get_similar_asset_ids(source.id))

    def test_get_similar_asset_ids_respects_limit(self):
        source = self._make_recitation(reciter=self.reciter_1, riwayah=self.riwayah_a)
        for _ in range(3):
            self._make_recitation(reciter=self.reciter_1, riwayah=self.riwayah_a)

        compute_similar_recommendations()
        result = get_similar_asset_ids(source.id, limit=2)

        self.assertEqual(2, len(result))

    def test_hydrate_drops_ids_no_longer_visible_and_preserves_order(self):
        source = self._make_recitation(reciter=self.reciter_1, riwayah=self.riwayah_a)
        visible = self._make_recitation(reciter=self.reciter_1, riwayah=self.riwayah_a)
        now_restricted = self._make_recitation(reciter=self.reciter_1, riwayah=self.riwayah_a)

        # Simulate the asset having become restricted after the nightly run computed it.
        ordered_ids = [now_restricted.id, visible.id]
        now_restricted.restricted_for_tenant = True
        now_restricted.save()

        hydrated = hydrate_visible_assets_in_order(ordered_ids)

        self.assertEqual([visible.id], [a.id for a in hydrated])
        self.assertNotIn(source.id, [a.id for a in hydrated])
