"""Schema serializers for testing endpoints to aid API documentation."""

from __future__ import annotations

from rest_framework import serializers

from .serializers import ChunkPlanSerializer, IndexProfileSerializer


class BundlePreviewItemSerializer(serializers.Serializer):
    sequence = serializers.IntegerField()
    title = serializers.CharField(allow_blank=True)
    component_ids = serializers.ListField(child=serializers.IntegerField(min_value=1))
    span_paths = serializers.ListField(child=serializers.CharField())
    text = serializers.CharField()
    token_count = serializers.IntegerField(min_value=0)


class BundlePreviewResponseSerializer(serializers.Serializer):
    plan_id = serializers.CharField()
    bundle_count = serializers.IntegerField()
    bundles = BundlePreviewItemSerializer(many=True)


class BundleExecuteResponseSerializer(serializers.Serializer):
    plan_id = serializers.CharField()
    profile = serializers.CharField()
    status = serializers.CharField()
    bundle_count = serializers.IntegerField()
    chunk_count = serializers.IntegerField()
    detail = serializers.CharField(allow_blank=True)


class ChunkPreviewItemSerializer(serializers.Serializer):
    sequence = serializers.IntegerField()
    bundle_sequence = serializers.IntegerField()
    local_sequence = serializers.IntegerField()
    text = serializers.CharField()
    token_count = serializers.IntegerField()
    char_start = serializers.IntegerField()
    char_end = serializers.IntegerField()


class ChunkPreviewResponseSerializer(serializers.Serializer):
    plan_id = serializers.CharField()
    bundle_count = serializers.IntegerField()
    chunk_count = serializers.IntegerField()
    chunk_size = serializers.IntegerField()
    overlap = serializers.IntegerField()
    bundles = BundlePreviewItemSerializer(many=True)
    chunks = ChunkPreviewItemSerializer(many=True)


class CreatePlansRequestSerializer(serializers.Serializer):
    paper_id = serializers.UUIDField()
    profile_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=True,
        required=False,
    )
    enqueue_bundles = serializers.BooleanField(default=False)


class CreatePlansResponseSerializer(serializers.Serializer):
    paper_id = serializers.IntegerField()
    plan_count = serializers.IntegerField()
    plans = ChunkPlanSerializer(many=True)


class ChunkPreviewRequestSerializer(serializers.Serializer):
    chunk_size = serializers.IntegerField(min_value=1, required=False)
    overlap = serializers.IntegerField(min_value=0, required=False)


class EnqueueEmbeddingResponseSerializer(serializers.Serializer):
    plan_id = serializers.CharField()
    status = serializers.CharField()
    chunk_ids = serializers.ListField(child=serializers.IntegerField())
    batches = serializers.ListField(
        child=serializers.ListField(child=serializers.IntegerField()),
    )
    detail = serializers.CharField(allow_blank=True)


class EmbeddingBatchRequestSerializer(serializers.Serializer):
    chunk_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=True,
        required=False,
    )


class EmbeddingPreviewChunkSerializer(serializers.Serializer):
    chunk_id = serializers.IntegerField()
    sequence = serializers.IntegerField()
    token_count = serializers.IntegerField()
    vector_dimensions = serializers.IntegerField()
    vector_preview = serializers.ListField(
        child=serializers.FloatField(),
        allow_empty=True,
    )


class EmbeddingPreviewResponseSerializer(serializers.Serializer):
    plan_id = serializers.CharField()
    embedded = serializers.IntegerField()
    chunks = EmbeddingPreviewChunkSerializer(many=True)


class EmbeddingExecuteResponseSerializer(serializers.Serializer):
    plan_id = serializers.CharField()
    status = serializers.CharField()
    embedded = serializers.IntegerField()


class PlanStatusResponseSerializer(serializers.Serializer):
    plan_id = serializers.CharField()
    status = serializers.CharField()
    bundle_count = serializers.IntegerField()
    chunk_count = serializers.IntegerField()
    embedded_chunks = serializers.IntegerField()
    failed_chunks = serializers.IntegerField()
    pending_chunks = serializers.IntegerField()
    last_error = serializers.CharField(allow_blank=True)
    is_active = serializers.BooleanField()


class PlanResetResponseSerializer(serializers.Serializer):
    plan_id = serializers.CharField()
    status = serializers.CharField()
    bundle_deleted = serializers.IntegerField()
    chunk_deleted = serializers.IntegerField()


class IndexProfileCreateResponseSerializer(IndexProfileSerializer):
    """Alias for documenting profile creation responses."""

    pass


class HFProfileCreateRequestSerializer(serializers.Serializer):
    model_id = serializers.CharField()


class QdrantHealthResponseSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    status = serializers.CharField(allow_blank=True, required=False)
    version = serializers.CharField(allow_blank=True, required=False)
    commit = serializers.CharField(allow_blank=True, required=False)
    detail = serializers.CharField(allow_blank=True, required=False)


class QdrantCollectionSerializer(serializers.Serializer):
    name = serializers.CharField()
    status = serializers.CharField(allow_blank=True, required=False)
    vectors_count = serializers.IntegerField(required=False, allow_null=True)
    points_count = serializers.IntegerField(required=False, allow_null=True)
    segments_count = serializers.IntegerField(required=False, allow_null=True)
    config = serializers.DictField(required=False, allow_empty=True)


class QdrantCollectionsResponseSerializer(serializers.Serializer):
    collections = QdrantCollectionSerializer(many=True)


class QdrantEnsureCollectionRequestSerializer(serializers.Serializer):
    profile_id = serializers.IntegerField(min_value=1)


class QdrantEnsureCollectionResponseSerializer(serializers.Serializer):
    profile_id = serializers.IntegerField()
    collection = serializers.CharField()
    detail = serializers.CharField()


class QdrantPointSerializer(serializers.Serializer):
    id = serializers.CharField()
    payload = serializers.DictField(allow_empty=True)


class QdrantPlanPointsResponseSerializer(serializers.Serializer):
    collection = serializers.CharField()
    plan_id = serializers.CharField()
    limit = serializers.IntegerField()
    returned = serializers.IntegerField()
    total = serializers.IntegerField()
    next_offset = serializers.CharField(allow_null=True, required=False)
    points = QdrantPointSerializer(many=True)


class QdrantPlanDeleteResponseSerializer(serializers.Serializer):
    plan_id = serializers.CharField()
    collection = serializers.CharField()
    detail = serializers.CharField()


class QdrantAutoSearchRequestSerializer(serializers.Serializer):
    text = serializers.CharField()
    profile_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=True,
    )
    profile_limit = serializers.IntegerField(required=False, min_value=1)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=100)
    score_threshold = serializers.FloatField(required=False)
    with_payload = serializers.BooleanField(required=False, default=True)
    with_vector_preview = serializers.BooleanField(required=False, default=False)


class QdrantAutoSearchMatchSerializer(serializers.Serializer):
    id = serializers.CharField()
    score = serializers.FloatField()
    chunk_id = serializers.IntegerField(required=False, allow_null=True)
    plan_id = serializers.CharField(required=False, allow_blank=True)
    chunk_sequence = serializers.IntegerField(required=False, allow_null=True)
    bundle_sequence = serializers.IntegerField(required=False, allow_null=True)
    token_count = serializers.IntegerField(required=False, allow_null=True)
    paper_id = serializers.CharField(required=False, allow_blank=True)
    payload = serializers.DictField(required=False, allow_empty=True)
    vector_preview = serializers.ListField(
        child=serializers.FloatField(),
        required=False,
        allow_empty=True,
    )
    chunk_text = serializers.CharField(required=False, allow_blank=True)


class QdrantAutoSearchProfileResultSerializer(serializers.Serializer):
    profile_id = serializers.IntegerField()
    profile_slug = serializers.CharField()
    collection = serializers.CharField()
    used_tokens = serializers.IntegerField()
    returned = serializers.IntegerField()
    matches = QdrantAutoSearchMatchSerializer(many=True)
    embedding_time_ms = serializers.FloatField(required=False)
    search_time_ms = serializers.FloatField(required=False)
    db_time_ms = serializers.FloatField(required=False)


class QdrantAutoSearchResponseSerializer(serializers.Serializer):
    query_tokens = serializers.IntegerField()
    limit = serializers.IntegerField()
    score_threshold = serializers.FloatField(allow_null=True)
    profiles_checked = QdrantAutoSearchProfileResultSerializer(many=True)
    warnings = serializers.ListField(
        child=serializers.CharField(),
        allow_empty=True,
    )
    elapsed_ms = serializers.FloatField()
