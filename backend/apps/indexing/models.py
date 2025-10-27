"""Database tables backing the indexing pipeline."""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from backend.apps.pastpaper.models import PastPaper, PastPaperComponent


class IndexProfile(models.Model):
    """Immutable configuration describing an embedding/indexing strategy."""

    slug = models.SlugField(max_length=64, unique=True)
    display_name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")

    encoder = models.CharField(max_length=128)
    tokenizer = models.CharField(max_length=128)
    dimension = models.PositiveIntegerField()
    max_input_tokens = models.PositiveIntegerField()

    chunk_size = models.PositiveIntegerField()
    chunk_overlap = models.PositiveIntegerField(default=0)
    target_bundle_tokens = models.PositiveIntegerField(default=600)

    qdrant_collection = models.CharField(max_length=128)
    qdrant_distance = models.CharField(max_length=32, default="Cosine")
    hnsw_m = models.PositiveIntegerField(default=32)
    hnsw_ef_construct = models.PositiveIntegerField(default=200)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["is_active"], name="idx_profile_active"),
            models.Index(fields=["qdrant_collection"], name="idx_profile_collection"),
        ]

    def __str__(self) -> str:
        return f"IndexProfile({self.slug}, encoder={self.encoder}, dim={self.dimension})"


class ChunkPlanStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    BUNDLING = "BUNDLING", "Bundling"
    READY_FOR_EMBEDDING = "READY", "Ready for embedding"
    EMBEDDING = "EMBEDDING", "Embedding"
    EMBEDDED = "EMBEDDED", "Embedded"
    FAILED = "FAILED", "Failed"


class ChunkPlan(models.Model):
    """Represents the application of an IndexProfile to a specific PastPaper version."""

    plan_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    paper = models.ForeignKey(PastPaper, on_delete=models.CASCADE, related_name="chunk_plans")
    profile = models.ForeignKey(IndexProfile, on_delete=models.PROTECT, related_name="plans")

    status = models.CharField(
        max_length=16,
        choices=ChunkPlanStatus.choices,
        default=ChunkPlanStatus.PENDING,
        db_index=True,
    )
    last_error = models.TextField(blank=True, default="")

    bundle_count = models.PositiveIntegerField(default=0)
    chunk_count = models.PositiveIntegerField(default=0)

    is_active = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    bundled_at = models.DateTimeField(null=True, blank=True)
    embedded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["paper", "profile"], name="idx_plan_paper_profile"),
            models.Index(fields=["is_active"], name="idx_plan_active"),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"ChunkPlan({self.plan_id}, paper={self.paper_id}, profile={self.profile_id}, status={self.status})"


class Bundle(models.Model):
    """Coarse semantic unit generated from the PastPaper component tree."""

    plan = models.ForeignKey(ChunkPlan, on_delete=models.CASCADE, related_name="bundles")
    root_component = models.ForeignKey(
        PastPaperComponent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bundle_roots",
    )

    sequence = models.PositiveIntegerField()
    title = models.CharField(max_length=256, blank=True, default="")
    component_ids = models.JSONField(default=list, blank=True)
    span_paths = models.JSONField(default=list, blank=True)

    text = models.TextField()
    token_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("plan", "sequence")]
        indexes = [
            models.Index(fields=["plan", "sequence"], name="idx_bundle_plan_seq"),
        ]
        ordering = ["sequence"]

    def __str__(self) -> str:
        return f"Bundle(plan={self.plan_id}, seq={self.sequence}, tokens={self.token_count})"


class ChunkEmbeddingStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    QUEUED = "QUEUED", "Queued"
    EMBEDDING = "EMBEDDING", "Embedding"
    EMBEDDED = "EMBEDDED", "Embedded"
    FAILED = "FAILED", "Failed"


class Chunk(models.Model):
    """Final chunk that will be embedded and pushed into the vector store."""

    plan = models.ForeignKey(ChunkPlan, on_delete=models.CASCADE, related_name="chunks")
    bundle = models.ForeignKey(Bundle, on_delete=models.CASCADE, related_name="chunks")

    sequence = models.PositiveIntegerField()
    text = models.TextField()
    token_count = models.PositiveIntegerField(default=0)
    char_start = models.PositiveIntegerField(default=0)
    char_end = models.PositiveIntegerField(default=0)

    embedding_status = models.CharField(
        max_length=16,
        choices=ChunkEmbeddingStatus.choices,
        default=ChunkEmbeddingStatus.PENDING,
        db_index=True,
    )
    qdrant_point_id = models.CharField(max_length=128, blank=True, default="")
    embedded_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("plan", "sequence")]
        indexes = [
            models.Index(fields=["plan", "sequence"], name="idx_chunk_plan_seq"),
            models.Index(fields=["embedding_status"], name="idx_chunk_status"),
        ]
        ordering = ["sequence"]

    def __str__(self) -> str:
        return f"Chunk(plan={self.plan_id}, seq={self.sequence}, status={self.embedding_status})"
