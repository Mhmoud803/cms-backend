from datetime import timedelta

from django.utils import timezone
from model_bakery import baker

from apps.content.models import (
    Asset,
    AssetVersion,
    AssetVersionEntry,
    CategoryChoice,
    StatusChoice,
    VersionStateChoice,
)
from apps.content.tasks import cleanup_abandoned_content_drafts_task
from apps.core.permissions import PermissionChoice
from apps.core.tests.base import BaseTestCase
from apps.publishers.models import Publisher
from apps.quran.models import Ayah, Sura
from apps.users.models import User


class AssetContentBaseTest(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.publisher = baker.make(Publisher, name="Test Publisher")
        self.translation = baker.make(
            Asset,
            category=CategoryChoice.TRANSLATION,
            publisher=self.publisher,
            status=StatusChoice.READY,
            name="French Rashid",
            slug="french-rashid",
        )
        self.user = User.objects.create_user(
            email="editor@example.com", name="Editor", is_staff=True
        )
        # A tiny corpus: sura 1 with 3 ayahs.
        self.sura = baker.make(Sura, id=1, name="الفاتحة", ayas_count=3)
        self.ayahs = [
            baker.make(Ayah, id=i, sura=self.sura, number_in_sura=i, text=f"ayah {i}")
            for i in (1, 2, 3)
        ]


class GetOrCreateDraftTest(AssetContentBaseTest):
    def test_get_or_create_draft_where_no_draft_exists_should_create_one(self):
        # Arrange
        self.authenticate_user(self.user)
        self.give_permission(self.user, PermissionChoice.PORTAL_UPDATE_TRANSLATION)

        # Act
        response = self.client.post(
            f"/portal/content/translations/{self.translation.slug}/draft/"
        )

        # Assert
        self.assertEqual(200, response.status_code, response.content)
        body = response.json()
        self.assertEqual("draft", body["state"])
        self.assertEqual(
            1,
            AssetVersion.objects.filter(
                asset=self.translation, state=VersionStateChoice.DRAFT
            ).count(),
        )

    def test_get_or_create_draft_where_draft_exists_should_return_same_draft(self):
        # Arrange
        self.authenticate_user(self.user)
        self.give_permission(self.user, PermissionChoice.PORTAL_UPDATE_TRANSLATION)
        existing = baker.make(
            AssetVersion, asset=self.translation, state=VersionStateChoice.DRAFT
        )

        # Act
        response = self.client.post(
            f"/portal/content/translations/{self.translation.slug}/draft/"
        )

        # Assert
        self.assertEqual(200, response.status_code, response.content)
        self.assertEqual(existing.id, response.json()["id"])
        self.assertEqual(
            1,
            AssetVersion.objects.filter(
                asset=self.translation, state=VersionStateChoice.DRAFT
            ).count(),
        )

    def test_get_or_create_draft_where_published_exists_should_seed_entries(self):
        # Arrange
        self.authenticate_user(self.user)
        self.give_permission(self.user, PermissionChoice.PORTAL_UPDATE_TRANSLATION)
        published = baker.make(
            AssetVersion, asset=self.translation, state=VersionStateChoice.PUBLISHED
        )
        baker.make(AssetVersionEntry, version=published, ayah=self.ayahs[0], text="hello")
        baker.make(AssetVersionEntry, version=published, ayah=self.ayahs[1], text="world")

        # Act
        response = self.client.post(
            f"/portal/content/translations/{self.translation.slug}/draft/"
        )

        # Assert
        self.assertEqual(200, response.status_code, response.content)
        draft = AssetVersion.objects.get(
            asset=self.translation, state=VersionStateChoice.DRAFT
        )
        self.assertEqual(2, draft.entries.count())

    def test_get_or_create_draft_where_user_lacks_permission_should_return_403(self):
        # Arrange
        self.authenticate_user(self.user)

        # Act
        response = self.client.post(
            f"/portal/content/translations/{self.translation.slug}/draft/"
        )

        # Assert
        self.assertEqual(403, response.status_code)
        self.assertEqual("permission_denied", response.json()["error_name"])


class PatchEntriesTest(AssetContentBaseTest):
    def _make_draft(self) -> AssetVersion:
        return baker.make(
            AssetVersion, asset=self.translation, state=VersionStateChoice.DRAFT
        )

    def test_patch_entries_where_new_rows_should_create_entries(self):
        # Arrange
        self.authenticate_user(self.user)
        self.give_permission(self.user, PermissionChoice.PORTAL_UPDATE_TRANSLATION)
        draft = self._make_draft()

        # Act
        response = self.client.patch(
            f"/portal/content/translations/{self.translation.slug}/versions/{draft.id}/entries/",
            data={
                "rows": [
                    {"ayah_id": 1, "text": "au nom", "footnotes": "[note]"},
                    {"ayah_id": 2, "text": "louange"},
                ]
            },
            content_type="application/json",
        )

        # Assert
        self.assertEqual(200, response.status_code, response.content)
        self.assertEqual(2, draft.entries.count())
        entry = draft.entries.get(ayah_id=1)
        self.assertEqual("au nom", entry.text)
        self.assertEqual("[note]", entry.footnotes)

    def test_patch_entries_where_existing_row_should_update_text(self):
        # Arrange
        self.authenticate_user(self.user)
        self.give_permission(self.user, PermissionChoice.PORTAL_UPDATE_TRANSLATION)
        draft = self._make_draft()
        baker.make(AssetVersionEntry, version=draft, ayah=self.ayahs[0], text="old")

        # Act
        response = self.client.patch(
            f"/portal/content/translations/{self.translation.slug}/versions/{draft.id}/entries/",
            data={"rows": [{"ayah_id": 1, "text": "new"}]},
            content_type="application/json",
        )

        # Assert
        self.assertEqual(200, response.status_code, response.content)
        self.assertEqual(1, draft.entries.count())
        self.assertEqual("new", draft.entries.get(ayah_id=1).text)

    def test_patch_entries_where_version_is_published_should_return_400(self):
        # Arrange
        self.authenticate_user(self.user)
        self.give_permission(self.user, PermissionChoice.PORTAL_UPDATE_TRANSLATION)
        published = baker.make(
            AssetVersion, asset=self.translation, state=VersionStateChoice.PUBLISHED
        )

        # Act
        response = self.client.patch(
            f"/portal/content/translations/{self.translation.slug}/versions/{published.id}/entries/",
            data={"rows": [{"ayah_id": 1, "text": "x"}]},
            content_type="application/json",
        )

        # Assert
        self.assertEqual(400, response.status_code, response.content)
        self.assertEqual("version_not_editable", response.json()["error_name"])


class PublishDraftTest(AssetContentBaseTest):
    def test_publish_draft_where_valid_should_become_latest_published(self):
        # Arrange
        self.authenticate_user(self.user)
        self.give_permission(self.user, PermissionChoice.PORTAL_UPDATE_TRANSLATION)
        draft = baker.make(
            AssetVersion, asset=self.translation, state=VersionStateChoice.DRAFT
        )
        baker.make(AssetVersionEntry, version=draft, ayah=self.ayahs[0], text="text")

        # Act
        response = self.client.post(
            f"/portal/content/translations/{self.translation.slug}/versions/{draft.id}/publish/",
            data={"name": "V2"},
            content_type="application/json",
        )

        # Assert
        self.assertEqual(200, response.status_code, response.content)
        draft.refresh_from_db()
        self.assertEqual(VersionStateChoice.PUBLISHED, draft.state)
        self.assertEqual(draft.id, self.translation.get_latest_version().id)

    def test_publish_draft_where_not_a_draft_should_return_400(self):
        # Arrange
        self.authenticate_user(self.user)
        self.give_permission(self.user, PermissionChoice.PORTAL_UPDATE_TRANSLATION)
        published = baker.make(
            AssetVersion, asset=self.translation, state=VersionStateChoice.PUBLISHED
        )

        # Act
        response = self.client.post(
            f"/portal/content/translations/{self.translation.slug}/versions/{published.id}/publish/",
            data={},
            content_type="application/json",
        )

        # Assert
        self.assertEqual(400, response.status_code, response.content)
        self.assertEqual("version_not_editable", response.json()["error_name"])


class DiscardDraftTest(AssetContentBaseTest):
    def test_discard_draft_where_valid_should_delete_draft_and_entries(self):
        # Arrange
        self.authenticate_user(self.user)
        self.give_permission(self.user, PermissionChoice.PORTAL_UPDATE_TRANSLATION)
        draft = baker.make(
            AssetVersion, asset=self.translation, state=VersionStateChoice.DRAFT
        )
        baker.make(AssetVersionEntry, version=draft, ayah=self.ayahs[0], text="x")

        # Act
        response = self.client.delete(
            f"/portal/content/translations/{self.translation.slug}/versions/{draft.id}/"
        )

        # Assert
        self.assertEqual(204, response.status_code, response.content)
        self.assertFalse(AssetVersion.objects.filter(id=draft.id).exists())
        self.assertEqual(0, AssetVersionEntry.objects.filter(version_id=draft.id).count())


class DraftExclusionTest(AssetContentBaseTest):
    def test_get_latest_version_where_only_draft_exists_should_return_none(self):
        # Arrange
        baker.make(AssetVersion, asset=self.translation, state=VersionStateChoice.DRAFT)

        # Act
        latest = self.translation.get_latest_version()

        # Assert
        self.assertIsNone(latest)

    def test_list_versions_where_draft_present_should_exclude_draft(self):
        # Arrange
        self.authenticate_user(self.user)
        self.give_permission(self.user, PermissionChoice.PORTAL_READ_TRANSLATION)
        baker.make(
            AssetVersion, asset=self.translation, name="V1", state=VersionStateChoice.PUBLISHED
        )
        baker.make(
            AssetVersion, asset=self.translation, name="D", state=VersionStateChoice.DRAFT
        )

        # Act
        response = self.client.get(
            f"/portal/translations/{self.translation.slug}/versions/"
        )

        # Assert
        self.assertEqual(200, response.status_code, response.content)
        names = [v["name"] for v in response.json()["results"]]
        self.assertEqual(["V1"], names)


class TafsirContentTest(AssetContentBaseTest):
    def setUp(self):
        super().setUp()
        self.tafsir = baker.make(
            Asset,
            category=CategoryChoice.TAFSIR,
            publisher=self.publisher,
            status=StatusChoice.READY,
            name="Tabari",
            slug="tabari",
        )

    def test_get_or_create_draft_where_tafsir_should_create_draft(self):
        # Arrange
        self.authenticate_user(self.user)
        self.give_permission(self.user, PermissionChoice.PORTAL_UPDATE_TAFSIR)

        # Act
        response = self.client.post(
            f"/portal/content/tafsirs/{self.tafsir.slug}/draft/"
        )

        # Assert
        self.assertEqual(200, response.status_code, response.content)
        self.assertEqual("draft", response.json()["state"])

    def test_get_or_create_draft_where_only_translation_permission_should_return_403(self):
        # Arrange
        self.authenticate_user(self.user)
        self.give_permission(self.user, PermissionChoice.PORTAL_UPDATE_TRANSLATION)

        # Act — translation permission must NOT grant tafsir editing
        response = self.client.post(
            f"/portal/content/tafsirs/{self.tafsir.slug}/draft/"
        )

        # Assert
        self.assertEqual(403, response.status_code, response.content)
        self.assertEqual("permission_denied", response.json()["error_name"])

    def test_get_or_create_draft_where_unsupported_category_should_return_404(self):
        # Arrange
        self.authenticate_user(self.user)
        self.give_permission(self.user, PermissionChoice.PORTAL_UPDATE_TAFSIR)

        # Act
        response = self.client.post(
            f"/portal/content/recitations/{self.tafsir.slug}/draft/"
        )

        # Assert
        self.assertEqual(404, response.status_code, response.content)
        self.assertEqual("unsupported_content_category", response.json()["error_name"])


class CleanupAbandonedDraftsTaskTest(AssetContentBaseTest):
    def test_cleanup_where_draft_is_stale_should_delete_it(self):
        # Arrange
        stale = baker.make(
            AssetVersion, asset=self.translation, state=VersionStateChoice.DRAFT
        )
        AssetVersion.objects.filter(pk=stale.pk).update(
            updated_at=timezone.now() - timedelta(hours=48)
        )

        # Act
        result = cleanup_abandoned_content_drafts_task(older_than_hours=24)

        # Assert
        self.assertEqual(1, result["deleted"])
        self.assertFalse(AssetVersion.objects.filter(pk=stale.pk).exists())

    def test_cleanup_where_draft_is_recent_should_keep_it(self):
        # Arrange
        fresh = baker.make(
            AssetVersion, asset=self.translation, state=VersionStateChoice.DRAFT
        )

        # Act
        result = cleanup_abandoned_content_drafts_task(older_than_hours=24)

        # Assert
        self.assertEqual(0, result["deleted"])
        self.assertTrue(AssetVersion.objects.filter(pk=fresh.pk).exists())

    def test_cleanup_where_version_is_published_should_keep_it(self):
        # Arrange
        published = baker.make(
            AssetVersion, asset=self.translation, state=VersionStateChoice.PUBLISHED
        )
        AssetVersion.objects.filter(pk=published.pk).update(
            updated_at=timezone.now() - timedelta(hours=48)
        )

        # Act
        result = cleanup_abandoned_content_drafts_task(older_than_hours=24)

        # Assert
        self.assertEqual(0, result["deleted"])
        self.assertTrue(AssetVersion.objects.filter(pk=published.pk).exists())
