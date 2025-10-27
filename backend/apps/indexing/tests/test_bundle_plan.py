"""Focused tests for the `bundle_plan` Dramatiq actor."""

from __future__ import annotations

import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.utils import override_settings

from backend.apps.indexing.models import (
    ChunkPlan,
    ChunkPlanStatus,
    IndexProfile,
)
from backend.apps.indexing.tasks import bundle_plan, enqueue_embedding_plan
from backend.apps.pastpaper.models import (
    PastPaper,
    PastPaperAsset,
    PastPaperComponent,
    PastPaperMetadata,
)


class BundlePlanTests(TestCase):
    """Exercise critical paths of the bundle_plan actor."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_dir = tempfile.mkdtemp(prefix="indexing-tests-media-")
        cls._override = override_settings(MEDIA_ROOT=cls._media_dir)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media_dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.metadata = PastPaperMetadata.objects.create(
            paper_code="9489_w24_qp_11",
            exam_board="CAIE",
            subject="Example Subject",
            syllabus_code="9489",
            season="Winter",
            year=2024,
            variant_no="11",
            paper_type="qp",
        )
        upload = SimpleUploadedFile("sample.pdf", b"%PDF-1.4\n", content_type="application/pdf")
        self.asset = PastPaperAsset.objects.create(
            file=upload,
            checksum_sha256="0" * 64,
        )
        self.addCleanup(lambda: self.asset.file.delete(save=False))

        self.paper = PastPaper.objects.create(
            metadata=self.metadata,
            asset=self.asset,
        )
        self.profile = IndexProfile.objects.create(
            slug="default-profile",
            display_name="Default Profile",
            description="Profile for tests",
            encoder="text-encoder",
            tokenizer="test-tokenizer",
            dimension=1024,
            max_input_tokens=8000,
            chunk_size=256,
            chunk_overlap=32,
            target_bundle_tokens=512,
            qdrant_collection="test-collection",
            qdrant_distance="Cosine",
            hnsw_m=16,
            hnsw_ef_construct=200,
            is_active=True,
        )

    def _create_plan(self) -> ChunkPlan:
        return ChunkPlan.objects.create(
            paper=self.paper,
            profile=self.profile,
        )

    def test_bundle_plan_without_components_marks_failed(self):
        """When no components exist, the plan is marked failed with a helpful error."""

        plan = self._create_plan()

        bundle_plan.fn(plan.id)

        plan.refresh_from_db()
        self.assertEqual(plan.status, ChunkPlanStatus.FAILED)
        self.assertEqual(plan.last_error, "No components available for bundling")
        self.assertEqual(plan.bundle_count, 0)
        self.assertEqual(plan.chunk_count, 0)

    def test_bundle_plan_successful_generation_creates_bundles_and_chunks(self):
        """A successful bundling run persists bundles/chunks and enqueues embedding."""

        component = PastPaperComponent.objects.create(
            paper=self.paper,
            num_display="1",
            content="Question content",
        )
        plan = self._create_plan()

        bundle_spec = SimpleNamespace(
            sequence=1,
            component_ids=[component.id],
            span_paths=[["1"]],
            title="Question 1",
            text="Bundle text",
            token_count=123,
        )
        chunk_spec = SimpleNamespace(
            text="Chunk text",
            token_count=120,
            char_start=0,
            char_end=60,
        )

        tokenizer_stub = object()
        with (
            patch("backend.apps.indexing.tasks.get_tokenizer", return_value=tokenizer_stub) as mock_tokenizer,
            patch("backend.apps.indexing.tasks.build_bundles", return_value=[bundle_spec]) as mock_build,
            patch("backend.apps.indexing.tasks.split_bundle", return_value=[chunk_spec]) as mock_split,
            patch.object(enqueue_embedding_plan, "send") as mock_enqueue,
        ):
            bundle_plan.fn(plan.id)

        plan.refresh_from_db()
        self.assertEqual(plan.status, ChunkPlanStatus.READY_FOR_EMBEDDING)
        self.assertEqual(plan.bundle_count, 1)
        self.assertEqual(plan.chunk_count, 1)
        self.assertIsNotNone(plan.bundled_at)
        mock_enqueue.assert_called_once_with(plan.id)

        bundles = list(plan.bundles.all())
        self.assertEqual(len(bundles), 1)
        bundle = bundles[0]
        self.assertEqual(bundle.sequence, 1)
        self.assertEqual(bundle.title, "Question 1")
        self.assertEqual(bundle.component_ids, [component.id])
        self.assertEqual(bundle.token_count, 123)

        chunks = list(plan.chunks.all())
        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertEqual(chunk.sequence, 1)
        self.assertEqual(chunk.bundle_id, bundle.id)
        self.assertEqual(chunk.text, "Chunk text")
        self.assertEqual(chunk.token_count, 120)

        mock_tokenizer.assert_called_once_with("test-tokenizer")
        build_args, build_kwargs = mock_build.call_args
        self.assertEqual(build_args[0], plan.paper)
        self.assertEqual(build_kwargs["tokenizer"], tokenizer_stub)
        self.assertEqual(build_kwargs["target_tokens"], plan.profile.target_bundle_tokens)

        split_args, split_kwargs = mock_split.call_args
        self.assertEqual(split_args[0], bundle_spec)
        self.assertEqual(split_kwargs["tokenizer"], tokenizer_stub)
        self.assertEqual(split_kwargs["chunk_size"], plan.profile.chunk_size)
        self.assertEqual(split_kwargs["overlap"], plan.profile.chunk_overlap)
