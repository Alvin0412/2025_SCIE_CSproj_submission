from __future__ import annotations

from django.test import TestCase

from backend.apps.indexing.models import (
    Bundle,
    Chunk,
    ChunkPlan,
    ChunkPlanStatus,
    IndexProfile,
)
from backend.apps.indexing.tool import (
    fetch_chunks_by_ids,
    fetch_chunks_for_point_ids,
    list_active_indices,
)
from backend.apps.pastpaper.models import (
    PastPaper,
    PastPaperAsset,
    PastPaperMetadata,
)


class IndexToolTests(TestCase):
    def setUp(self) -> None:
        self.asset = PastPaperAsset.objects.create(
            file="pastpapers/index.pdf",
            mime="application/pdf",
            size=1,
            checksum_sha256="b" * 64,
        )
        self.profile_active = IndexProfile.objects.create(
            slug="active-profile",
            display_name="Active profile",
            description="",
            encoder="text-embedding",
            tokenizer="gpt",
            dimension=1536,
            max_input_tokens=8192,
            chunk_size=500,
            chunk_overlap=50,
            target_bundle_tokens=600,
            qdrant_collection="collection-active",
            qdrant_distance="Cosine",
            is_active=True,
        )
        self.profile_inactive = IndexProfile.objects.create(
            slug="inactive-profile",
            display_name="Inactive profile",
            description="",
            encoder="text-embedding",
            tokenizer="gpt",
            dimension=1536,
            max_input_tokens=8192,
            chunk_size=500,
            chunk_overlap=50,
            target_bundle_tokens=600,
            qdrant_collection="collection-inactive",
            qdrant_distance="Cosine",
            is_active=False,
        )

        self.metadata_recent = PastPaperMetadata.objects.create(
            paper_code="9708_w23_qp_12",
            exam_board="CAIE",
            subject="Economics",
            syllabus_code="9708",
            season="Winter",
            year=2023,
            paper_type="qp",
        )
        self.paper_recent = PastPaper.objects.create(
            metadata=self.metadata_recent,
            asset=self.asset,
            parsed_state="READY",
            is_active=True,
        )
        self.metadata_old = PastPaperMetadata.objects.create(
            paper_code="9708_w20_qp_11",
            exam_board="CAIE",
            subject="Economics",
            syllabus_code="9708",
            season="Winter",
            year=2020,
            paper_type="qp",
        )
        self.paper_old = PastPaper.objects.create(
            metadata=self.metadata_old,
            asset=self.asset,
            parsed_state="READY",
            is_active=True,
        )

        self.plan_recent = ChunkPlan.objects.create(
            paper=self.paper_recent,
            profile=self.profile_active,
            status=ChunkPlanStatus.EMBEDDED,
            is_active=True,
        )
        self.plan_old = ChunkPlan.objects.create(
            paper=self.paper_old,
            profile=self.profile_active,
            status=ChunkPlanStatus.EMBEDDED,
            is_active=True,
        )
        ChunkPlan.objects.create(  # inactive plan should never surface
            paper=self.paper_recent,
            profile=self.profile_inactive,
            status=ChunkPlanStatus.EMBEDDED,
            is_active=False,
        )

        self.bundle = Bundle.objects.create(
            plan=self.plan_recent,
            root_component=None,
            sequence=1,
            title="Bundle",
            component_ids=[1, 2],
            span_paths=["Q1", "Q2"],
            text="Sample bundle text",
            token_count=120,
        )
        self.chunk_one = Chunk.objects.create(
            plan=self.plan_recent,
            bundle=self.bundle,
            sequence=1,
            text="Alpha chunk content",
            token_count=60,
            qdrant_point_id="point-1",
        )
        self.chunk_two = Chunk.objects.create(
            plan=self.plan_recent,
            bundle=self.bundle,
            sequence=2,
            text="Beta chunk content",
            token_count=60,
            qdrant_point_id="point-2",
        )

    def test_list_active_indices_filters_inactive_and_limits(self):
        indices = list_active_indices(
            subject="Economics",
            exam_board="CAIE",
            syllabus_code="9708",
            year_from=2021,
            paper_type="qp",
            limit=1,
        )
        self.assertEqual(1, len(indices))
        self.assertEqual(2023, indices[0].year)
        self.assertEqual(str(self.paper_recent.paper_id), indices[0].paper_uuid)

    def test_fetch_chunks_for_point_ids_deduplicates_requests(self):
        records = fetch_chunks_for_point_ids(self.plan_recent.plan_id, ["point-1", "point-1", "missing"])
        self.assertEqual(1, len(records))
        self.assertEqual("point-1", records[0].qdrant_point_id)

    def test_fetch_chunks_by_ids_handles_duplicates(self):
        records = fetch_chunks_by_ids([self.chunk_one.id, self.chunk_one.id, self.chunk_two.id])
        self.assertEqual(2, len(records))
        returned_ids = {record.chunk_id for record in records}
        self.assertTrue({self.chunk_one.id, self.chunk_two.id}.issubset(returned_ids))

    def test_list_active_indices_relaxes_when_only_inactive_available(self):
        ChunkPlan.objects.filter(pk=self.plan_recent.pk).update(is_active=False)
        ChunkPlan.objects.filter(pk=self.plan_old.pk).update(is_active=False)
        indices = list_active_indices(subject="Economics", syllabus_code="9708", limit=5)
        self.assertEqual(2, len(indices))
        years = sorted(index.year for index in indices)
        self.assertEqual([2020, 2023], years)
