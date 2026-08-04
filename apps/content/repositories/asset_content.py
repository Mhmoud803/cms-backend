"""Data-access layer for per-ayah asset content editing (drafts + entries).

Shared by translations and tafsirs; both edit the same
``AssetVersion`` / ``AssetVersionEntry`` tables.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import QuerySet

from apps.content.models import Asset, AssetVersion, AssetVersionEntry, VersionStateChoice
from apps.content.services.asset_content_import import ParsedEntry
from apps.quran.models import Ayah


class AssetContentRepository:
    def __init__(self) -> None:
        self.asset_version_model = AssetVersion
        self.entry_model = AssetVersionEntry

    def _ayah_id_by_sura_aya(self) -> dict[tuple[int, int], int]:
        """Map (sura_id, number_in_sura) -> ayah pk for resolving parsed rows."""
        return {
            (sura_id, number): ayah_id
            for ayah_id, sura_id, number in Ayah.objects.values_list(
                "id", "sura_id", "number_in_sura"
            )
        }

    def get_draft(self, asset: Asset) -> AssetVersion | None:
        return self.asset_version_model.objects.filter(
            asset=asset, state=VersionStateChoice.DRAFT
        ).first()

    def get_version(self, asset: Asset, version_id: int) -> AssetVersion | None:
        return self.asset_version_model.objects.filter(asset=asset, id=version_id).first()

    def get_entries(self, version: AssetVersion) -> QuerySet[AssetVersionEntry]:
        return version.entries.select_related("ayah", "ayah__sura").order_by("order", "ayah_id")

    @transaction.atomic
    def create_draft_seeded_from(
        self,
        asset: Asset,
        source_version: AssetVersion | None,
        *,
        name: str,
        summary: str,
        created_by_id: int | None,
    ) -> AssetVersion:
        """Create a draft version, copying entries from ``source_version`` if given."""
        draft = self.asset_version_model.objects.create(
            asset=asset,
            name=name,
            summary=summary,
            state=VersionStateChoice.DRAFT,
            created_by_id=created_by_id,
        )
        if source_version is not None:
            copies = [
                AssetVersionEntry(
                    version=draft,
                    ayah_id=entry.ayah_id,
                    text=entry.text,
                    footnotes=entry.footnotes,
                    order=entry.order,
                )
                for entry in source_version.entries.all().iterator()
            ]
            if copies:
                AssetVersionEntry.objects.bulk_create(copies, batch_size=1000)
        return draft

    @transaction.atomic
    def replace_entries_from_parsed(
        self, version: AssetVersion, parsed: list[ParsedEntry]
    ) -> int:
        """Replace a version's entries with parsed per-ayah rows. Returns count."""
        ayah_index = self._ayah_id_by_sura_aya()
        version.entries.all().delete()
        rows: list[AssetVersionEntry] = []
        for parsed_entry in parsed:
            ayah_id = ayah_index.get((parsed_entry.sura, parsed_entry.aya))
            if ayah_id is None:
                continue
            rows.append(
                AssetVersionEntry(
                    version=version,
                    ayah_id=ayah_id,
                    text=parsed_entry.text,
                    footnotes=parsed_entry.footnotes,
                    order=ayah_id,
                )
            )
        if rows:
            AssetVersionEntry.objects.bulk_create(rows, batch_size=1000)
        return len(rows)

    @transaction.atomic
    def upsert_entries(
        self, version: AssetVersion, rows: list[dict[str, object]]
    ) -> list[AssetVersionEntry]:
        """Create or update draft entries keyed by ayah id. Returns changed rows."""
        ayah_ids = [int(row["ayah_id"]) for row in rows]
        existing = {
            entry.ayah_id: entry
            for entry in version.entries.filter(ayah_id__in=ayah_ids)
        }
        to_create: list[AssetVersionEntry] = []
        to_update: list[AssetVersionEntry] = []
        changed: list[AssetVersionEntry] = []

        for row in rows:
            ayah_id = int(row["ayah_id"])
            text = str(row.get("text", "") or "")
            footnotes = str(row.get("footnotes", "") or "")
            entry = existing.get(ayah_id)
            if entry is None:
                entry = AssetVersionEntry(
                    version=version,
                    ayah_id=ayah_id,
                    text=text,
                    footnotes=footnotes,
                    order=ayah_id,
                )
                to_create.append(entry)
            else:
                entry.text = text
                entry.footnotes = footnotes
                to_update.append(entry)
            changed.append(entry)

        if to_create:
            AssetVersionEntry.objects.bulk_create(to_create, batch_size=1000)
        if to_update:
            AssetVersionEntry.objects.bulk_update(
                to_update, ["text", "footnotes"], batch_size=1000
            )
        return changed

    @transaction.atomic
    def publish_draft(self, draft: AssetVersion) -> AssetVersion:
        """Flip a draft to published so newest-wins makes it the latest version."""
        draft.state = VersionStateChoice.PUBLISHED
        draft.save(update_fields=["state", "updated_at"])
        draft.asset.file_size = draft.human_readable_size
        draft.asset.save(update_fields=["file_size", "updated_at"])
        return draft

    def delete_version(self, version: AssetVersion) -> None:
        version.delete()
