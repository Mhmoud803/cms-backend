from typing import Literal

from django.http import Http404
from django.utils.translation import gettext_lazy as _
from ninja import Schema

from apps.content.models import Asset, StatusChoice
from apps.content.services.recommendations import get_similar_asset_ids, hydrate_visible_assets_in_order
from apps.core.ninja_utils.errors import NinjaErrorResponse
from apps.core.ninja_utils.request import Request
from apps.core.ninja_utils.router import ItqanRouter
from apps.core.ninja_utils.tags import NinjaTag
from apps.usage_tracking.decorators.track_usage import track_usage

router = ItqanRouter(tags=[NinjaTag.RECOMMENDATIONS])


class RecommendationPublisherOut(Schema):
    id: int
    name: str


class RecommendationReciterOut(Schema):
    id: int
    name: str


class RecommendationRiwayahOut(Schema):
    id: int
    name: str


class RecommendedAssetOut(Schema):
    id: int
    name: str
    slug: str
    category: str
    publisher: RecommendationPublisherOut
    reciter: RecommendationReciterOut | None = None
    riwayah: RecommendationRiwayahOut | None = None

    @staticmethod
    def resolve_publisher(obj):
        return {"id": obj.publisher_id, "name": obj.publisher.name} if obj.publisher_id else None

    @staticmethod
    def resolve_reciter(obj):
        return {"id": obj.reciter_id, "name": obj.reciter.name} if obj.reciter_id else None

    @staticmethod
    def resolve_riwayah(obj):
        return {"id": obj.riwayah_id, "name": obj.riwayah.name} if obj.riwayah_id else None


@router.get(
    "recommendations/similar/{asset_id}/",
    response={
        200: list[RecommendedAssetOut],
        404: NinjaErrorResponse[Literal["not_found"]],
    },
)
@track_usage(entity_type="recommendation_similar")
def get_similar_recommendations(request: Request, asset_id: int):
    """
    "Users who liked this also liked" — assets sharing reciter/riwayah/qiraah/category
    with `asset_id`, ranked by a precomputed similarity score (see
    apps.content.services.recommendations).

    404s only when the source asset itself doesn't exist or isn't publicly visible;
    an asset with zero matches simply returns an empty list, since "no similar content
    yet" is a valid, non-error outcome.

    This is discovery metadata (asset name/reciter/publisher), not audio content, so
    unlike /recitations/{asset_id}/ it doesn't require enforce_asset_access_on_public_api
    — it's readable the same way /reciters/ or /recitations/ (the list endpoint) are.
    """
    source_exists = Asset.objects.filter(
        id=asset_id,
        status=StatusChoice.READY,
        restricted_for_tenant=False,
    ).exists()
    if not source_exists:
        raise Http404(str(_("No asset matches the given query.")))

    similar_ids = get_similar_asset_ids(asset_id)
    return hydrate_visible_assets_in_order(similar_ids)
