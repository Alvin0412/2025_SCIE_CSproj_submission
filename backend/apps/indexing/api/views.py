"""ViewSets providing CRUD access to indexing models."""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from django.shortcuts import get_object_or_404
from django.utils.text import slugify
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema

from ..bundler import build_bundles
from ..chunker import split_bundle
from ..curd import reset_plan_state
from ..embedding import embed_texts
from ..hf_profiles import build_profile_defaults
from ..models import (
    Bundle,
    Chunk,
    ChunkEmbeddingStatus,
    ChunkPlan,
    ChunkPlanStatus,
    IndexProfile,
)
from ..tasks import (
    bundle_plan,
    bundle_plan_sync,
    create_plans_sync,
    embed_chunk_batch_sync,
    enqueue_embedding_plan_sync,
    _check_plan_completion,
)
from ..tokenization import get_tokenizer
from ..qdrant import (
    delete_plan as qdrant_delete_plan,
    describe_collection,
    ensure_collection as qdrant_ensure_collection,
    healthcheck as qdrant_healthcheck,
    list_collections,
    search_collection,
    summarize_plan_points,
)
from backend.apps.pastpaper.models import PastPaper

from .serializers import (
    BundleSerializer,
    ChunkPlanSerializer,
    ChunkSerializer,
    IndexProfileSerializer,
    ChunkPlanCreateSerializer,
)
from .schemas import (
    BundleExecuteResponseSerializer,
    BundlePreviewResponseSerializer,
    ChunkPreviewRequestSerializer,
    ChunkPreviewResponseSerializer,
    CreatePlansRequestSerializer,
    CreatePlansResponseSerializer,
    EmbeddingBatchRequestSerializer,
    EmbeddingExecuteResponseSerializer,
    EmbeddingPreviewResponseSerializer,
    EnqueueEmbeddingResponseSerializer,
    IndexProfileCreateResponseSerializer,
    HFProfileCreateRequestSerializer,
    PlanResetResponseSerializer,
    PlanStatusResponseSerializer,
    QdrantAutoSearchRequestSerializer,
    QdrantAutoSearchResponseSerializer,
    QdrantCollectionsResponseSerializer,
    QdrantEnsureCollectionRequestSerializer,
    QdrantEnsureCollectionResponseSerializer,
    QdrantHealthResponseSerializer,
    QdrantPlanDeleteResponseSerializer,
    QdrantPlanPointsResponseSerializer,
)


logger = logging.getLogger(__name__)


class IndexProfileViewSet(viewsets.ModelViewSet):
    """CRUD endpoints for index profile configurations."""

    serializer_class = IndexProfileSerializer
    queryset = IndexProfile.objects.all().order_by("-created_at")
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="is_active",
                location=OpenApiParameter.QUERY,
                required=False,
                type=bool,
                description="Filter profiles by active status (`true` or `false`).",
            ),
            OpenApiParameter(
                name="encoder",
                location=OpenApiParameter.QUERY,
                required=False,
                type=str,
                description="Return only profiles that use the specified encoder id.",
            ),
            OpenApiParameter(
                name="tokenizer",
                location=OpenApiParameter.QUERY,
                required=False,
                type=str,
                description="Return only profiles that use the specified tokenizer id.",
            ),
        ],
        responses=IndexProfileSerializer(many=True),
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        queryset = super().get_queryset()
        is_active = self.request.query_params.get("is_active")
        if is_active is not None:
            if is_active.lower() in {"true", "1"}:
                return queryset.filter(is_active=True)
            if is_active.lower() in {"false", "0"}:
                return queryset.filter(is_active=False)
        encoder = self.request.query_params.get("encoder")
        if encoder:
            queryset = queryset.filter(encoder=encoder)
        tokenizer = self.request.query_params.get("tokenizer")
        if tokenizer:
            queryset = queryset.filter(tokenizer=tokenizer)
        return queryset

    @extend_schema(
        request=HFProfileCreateRequestSerializer,
        responses={201: IndexProfileCreateResponseSerializer},
    )
    @action(detail=False, methods=["post"], url_path="from-hf")
    def create_from_hf(self, request):
        """Create an IndexProfile using metadata from a HuggingFace encoder."""

        payload = HFProfileCreateRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        model_id = data["model_id"]
        try:
            defaults = build_profile_defaults(model_id)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError({"detail": f"Failed to load HuggingFace model config: {exc}"})

        slug_value = self._resolve_slug(None, model_id)
        max_input_tokens = defaults.max_input_tokens
        chunk_size = defaults.chunk_size
        chunk_overlap = defaults.chunk_overlap
        target_bundle_tokens = defaults.target_bundle_tokens
        qdrant_collection = self._resolve_collection(provided=None, slug=slug_value)
        qdrant_distance = "Cosine"
        hnsw_m = 32
        hnsw_ef_construct = 200

        profile = IndexProfile.objects.create(
            slug=slug_value,
            display_name=model_id,
            description=f"Auto-generated profile for {model_id}",
            encoder=defaults.encoder_id,
            tokenizer=defaults.tokenizer_id,
            dimension=defaults.dimension,
            max_input_tokens=max_input_tokens,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            target_bundle_tokens=target_bundle_tokens,
            qdrant_collection=qdrant_collection,
            qdrant_distance=qdrant_distance,
            hnsw_m=hnsw_m,
            hnsw_ef_construct=hnsw_ef_construct,
            is_active=True,
        )

        response_serializer = IndexProfileCreateResponseSerializer(profile)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    def _resolve_slug(self, provided: str | None, model_id: str) -> str:
        """Derive a unique slug for the profile."""

        if provided:
            candidate = slugify(provided)
            if not candidate:
                raise ValidationError({"slug": "Provided slug is invalid after normalization."})
            if IndexProfile.objects.filter(slug=candidate).exists():
                raise ValidationError({"slug": "An IndexProfile with this slug already exists."})
            return candidate

        base = slugify(model_id.replace("/", "-"))
        if not base:
            base = f"hf-{uuid4().hex[:8]}"

        candidate = base
        suffix = 2
        while IndexProfile.objects.filter(slug=candidate).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def _resolve_collection(self, provided: str | None, slug: str) -> str:
        """Return a Qdrant collection name, ensuring no collisions with existing profiles."""

        if provided:
            return provided

        base = f"{slug}-collection"
        candidate = base
        suffix = 2
        while IndexProfile.objects.filter(qdrant_collection=candidate).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate


class ChunkPlanViewSet(viewsets.ModelViewSet):
    """CRUD endpoints for chunk plans."""

    serializer_class = ChunkPlanSerializer
    queryset = ChunkPlan.objects.select_related("paper", "profile").all()
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    def get_serializer_class(self):
        if self.action == "create":
            return ChunkPlanCreateSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        queryset = super().get_queryset()
        paper_param = self.request.query_params.get("paper")
        profile_param = self.request.query_params.get("profile")
        status_param = self.request.query_params.get("status")
        plan_uuid = self.request.query_params.get("plan_id")

        if paper_param:
            queryset = queryset.filter(paper_id=paper_param)
        if profile_param:
            queryset = queryset.filter(profile_id=profile_param)
        if status_param:
            queryset = queryset.filter(status=status_param)
        if plan_uuid:
            queryset = queryset.filter(plan_id=plan_uuid)

        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = serializer.save()
        read_serializer = ChunkPlanSerializer(plan, context=self.get_serializer_context())
        headers = self.get_success_headers(read_serializer.data)
        return Response(read_serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    @action(detail=True, methods=["post"], url_path="run-bundle")
    def run_bundle(self, request, pk=None):
        """Dispatch bundling for the selected plan via Dramatiq."""

        plan = self.get_object()
        bundle_plan.send(plan.id)
        return Response(
            {"detail": "bundle task enqueued", "plan_id": str(plan.plan_id)},
            status=status.HTTP_202_ACCEPTED,
        )


class BundleViewSet(viewsets.ModelViewSet):
    """CRUD endpoints for stored bundles."""

    serializer_class = BundleSerializer
    queryset = Bundle.objects.select_related("plan", "root_component").all()
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    def get_queryset(self):
        queryset = super().get_queryset()
        plan_param = self.request.query_params.get("plan")
        if plan_param:
            queryset = queryset.filter(plan_id=plan_param)
        return queryset


class ChunkViewSet(viewsets.ModelViewSet):
    """CRUD endpoints for individual chunks."""

    serializer_class = ChunkSerializer
    queryset = Chunk.objects.select_related("plan", "bundle").all()
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    def get_queryset(self):
        queryset = super().get_queryset()
        plan_param = self.request.query_params.get("plan")
        bundle_param = self.request.query_params.get("bundle")
        status_param = self.request.query_params.get("embedding_status")

        if plan_param:
            queryset = queryset.filter(plan_id=plan_param)
        if bundle_param:
            queryset = queryset.filter(bundle_id=bundle_param)
        if status_param:
            queryset = queryset.filter(embedding_status=status_param)

        return queryset


class IndexingTestingViewSet(viewsets.ViewSet):
    """Testing-only entrypoints for stepping through the indexing pipeline."""

    http_method_names = ["get", "post", "delete", "head", "options"]

    def _get_plan(self, pk: str) -> ChunkPlan:
        return get_object_or_404(
            ChunkPlan.objects.select_related("paper", "profile"),
            pk=pk,
        )

    @extend_schema(
        request=IndexProfileSerializer,
        responses={201: IndexProfileCreateResponseSerializer},
    )
    @action(detail=False, methods=["post"], url_path="profiles")
    def create_profile(self, request):
        """Create an IndexProfile via testing API."""

        serializer = IndexProfileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save()
        response_data = IndexProfileCreateResponseSerializer(profile).data
        return Response(response_data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=CreatePlansRequestSerializer,
        responses={201: CreatePlansResponseSerializer, 404: OpenApiResponse(description="Paper not found")},
    )
    @action(detail=False, methods=["post"], url_path="create-plans")
    def create_plans(self, request):
        """Synchronously create or reset chunk plans for a paper."""

        payload = CreatePlansRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        paper = get_object_or_404(PastPaper, paper_id=data["paper_id"])
        plans = create_plans_sync(
            paper_id=paper.paper_id,
            profile_ids=data.get("profile_ids"),
            enqueue_bundles=data.get("enqueue_bundles", False),
        )
        response_serializer = CreatePlansResponseSerializer(
            {
                "paper_id": paper.id,
                "plan_count": len(plans),
                "plans": plans,
            }
        )
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=None,
        responses={200: QdrantHealthResponseSerializer},
    )
    @action(detail=False, methods=["get"], url_path="qdrant/health")
    def qdrant_health(self, request):
        """Return the health status reported by Qdrant."""

        status_payload = qdrant_healthcheck()
        serializer = QdrantHealthResponseSerializer(status_payload)
        return Response(serializer.data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="name",
                location=OpenApiParameter.QUERY,
                required=False,
                type=str,
                description="Optional collection name. When supplied, only that collection is returned.",
            )
        ],
        responses={200: QdrantCollectionsResponseSerializer},
    )
    @action(detail=False, methods=["get"], url_path="qdrant/collections")
    def qdrant_collections(self, request):
        """Inspect Qdrant collections and their metadata."""

        collection_name = request.query_params.get("name")

        def _normalize(entry: dict[str, object]) -> dict[str, object]:
            config = entry.get("config") or {}
            if hasattr(config, "dict"):
                config = config.dict()
            elif hasattr(config, "model_dump"):
                config = config.model_dump()
            elif not isinstance(config, dict):
                config = {}
            return {
                "name": entry.get("name", ""),
                "status": entry.get("status"),
                "vectors_count": entry.get("vectors_count"),
                "points_count": entry.get("points_count"),
                "segments_count": entry.get("segments_count"),
                "config": config,
            }

        if collection_name:
            detail = describe_collection(collection_name)
            if detail is None:
                return Response(
                    {"detail": f"Collection '{collection_name}' not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            payload = {"collections": [_normalize(detail)]}
        else:
            payload = {"collections": []}
            for entry in list_collections():
                detail = describe_collection(entry.get("name", "")) or entry
                payload["collections"].append(_normalize(detail))

        serializer = QdrantCollectionsResponseSerializer(payload)
        return Response(serializer.data)

    @extend_schema(
        request=QdrantEnsureCollectionRequestSerializer,
        responses={200: QdrantEnsureCollectionResponseSerializer},
    )
    @action(detail=False, methods=["post"], url_path="qdrant/ensure-collection")
    def qdrant_ensure_collection(self, request):
        """Ensure the configured Qdrant collection exists for a profile."""

        payload = QdrantEnsureCollectionRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        profile = get_object_or_404(IndexProfile, pk=payload.validated_data["profile_id"])

        try:
            created = qdrant_ensure_collection(profile)
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"detail": f"Qdrant error while ensuring collection: {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        detail_message = "Collection created." if created else "Collection already existed."
        response_serializer = QdrantEnsureCollectionResponseSerializer(
            {
                "profile_id": profile.id,
                "collection": profile.qdrant_collection,
                "detail": detail_message,
            }
        )
        return Response(response_serializer.data)

    @extend_schema(
        request=None,
        responses={200: BundlePreviewResponseSerializer, 400: OpenApiResponse(description="Bundling failed")},
    )
    @action(detail=True, methods=["post"], url_path="bundle-preview")
    def bundle_preview(self, request, pk=None):
        """Compute bundle specs without persisting records."""

        plan = self._get_plan(pk)
        try:
            tokenizer = get_tokenizer(plan.profile.tokenizer)
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"detail": f"Tokenizer error: {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            bundle_specs = build_bundles(
                plan.paper,
                tokenizer=tokenizer,
                target_tokens=plan.profile.target_bundle_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"detail": f"Bundling failed: {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        response_serializer = BundlePreviewResponseSerializer(
            {
                "plan_id": str(plan.plan_id),
                "bundle_count": len(bundle_specs),
                "bundles": bundle_specs,
            }
        )
        return Response(response_serializer.data)

    @extend_schema(
        request=None,
        responses={
            200: BundleExecuteResponseSerializer,
            400: BundleExecuteResponseSerializer,
        },
    )
    @action(detail=True, methods=["post"], url_path="bundle-execute")
    def bundle_execute(self, request, pk=None):
        """Run the bundling pipeline synchronously and persist results."""

        plan = self._get_plan(pk)
        result = bundle_plan_sync(plan.id, enqueue_embedding=False)
        plan.refresh_from_db()
        status_code = status.HTTP_200_OK if result.get(
            "status") != ChunkPlanStatus.FAILED else status.HTTP_400_BAD_REQUEST
        response_serializer = BundleExecuteResponseSerializer(
            {
                "plan_id": str(plan.plan_id),
                "profile": plan.profile.slug,
                "status": plan.status,
                "bundle_count": plan.bundle_count,
                "chunk_count": plan.chunk_count,
                "detail": result.get("detail", ""),
            }
        )
        return Response(response_serializer.data, status=status_code)

    @extend_schema(
        request=ChunkPreviewRequestSerializer,
        responses={200: ChunkPreviewResponseSerializer, 400: OpenApiResponse(description="Chunk preview failed")},
    )
    @action(detail=True, methods=["post"], url_path="chunk-preview")
    def chunk_preview(self, request, pk=None):
        """Preview chunk output using optional overrides."""

        plan = self._get_plan(pk)
        payload = ChunkPreviewRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        overrides = payload.validated_data

        chunk_size = overrides.get("chunk_size", plan.profile.chunk_size)
        overlap = overrides.get("overlap", plan.profile.chunk_overlap)

        try:
            tokenizer = get_tokenizer(plan.profile.tokenizer)
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"detail": f"Tokenizer error: {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            bundle_specs = build_bundles(
                plan.paper,
                tokenizer=tokenizer,
                target_tokens=plan.profile.target_bundle_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"detail": f"Bundling failed: {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        chunk_entries: list[dict[str, object]] = []
        global_sequence = 1

        for bundle_spec in bundle_specs:
            try:
                chunk_specs = split_bundle(
                    bundle_spec,
                    tokenizer=tokenizer,
                    chunk_size=chunk_size,
                    overlap=overlap,
                    max_tokens=plan.profile.max_input_tokens,
                )
            except Exception as exc:  # noqa: BLE001
                return Response(
                    {
                        "detail": f"Chunking failed for bundle {bundle_spec.sequence}: {exc}",
                        "bundle_sequence": bundle_spec.sequence,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            for chunk_spec in chunk_specs:
                chunk_entries.append(
                    {
                        "sequence": global_sequence,
                        "bundle_sequence": bundle_spec.sequence,
                        "local_sequence": chunk_spec.sequence,
                        "text": chunk_spec.text,
                        "token_count": chunk_spec.token_count,
                        "char_start": chunk_spec.char_start,
                        "char_end": chunk_spec.char_end,
                    }
                )
                global_sequence += 1

        response_serializer = ChunkPreviewResponseSerializer(
            {
                "plan_id": str(plan.plan_id),
                "bundle_count": len(bundle_specs),
                "chunk_count": len(chunk_entries),
                "chunk_size": chunk_size,
                "overlap": overlap,
                "bundles": bundle_specs,
                "chunks": chunk_entries,
            }
        )
        return Response(response_serializer.data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="limit",
                location=OpenApiParameter.QUERY,
                required=False,
                type=int,
                description="Maximum number of points to return (1-256). Defaults to 50.",
            )
        ],
        responses={200: QdrantPlanPointsResponseSerializer, 400: OpenApiResponse(description="Qdrant summary failed")},
    )
    @action(detail=True, methods=["get"], url_path="qdrant-summary")
    def qdrant_summary(self, request, pk=None):
        """Return a snapshot of Qdrant points for the selected plan."""

        plan = self._get_plan(pk)
        limit_param = request.query_params.get("limit")
        limit = 50
        if limit_param is not None:
            try:
                limit = int(limit_param)
            except ValueError:
                raise ValidationError({"limit": "Limit must be an integer."})
            if limit <= 0:
                raise ValidationError({"limit": "Limit must be greater than zero."})

        try:
            summary = summarize_plan_points(plan.profile, plan.plan_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"detail": f"Failed to summarize Qdrant points: {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = QdrantPlanPointsResponseSerializer(summary)
        return Response(serializer.data)

    @extend_schema(
        request=None,
        responses={200: QdrantPlanDeleteResponseSerializer, 400: OpenApiResponse(description="Deletion failed")},
    )
    @action(detail=True, methods=["delete"], url_path="qdrant-points")
    def qdrant_delete_points(self, request, pk=None):
        """Delete Qdrant points associated with the selected plan."""

        plan = self._get_plan(pk)

        try:
            removed = qdrant_delete_plan(plan.profile, plan)
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"detail": f"Failed to delete Qdrant points: {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        detail_message = (
            "Qdrant points deleted for plan."
            if removed
            else "Collection missing or no points matched; nothing deleted."
        )
        serializer = QdrantPlanDeleteResponseSerializer(
            {
                "plan_id": str(plan.plan_id),
                "collection": plan.profile.qdrant_collection,
                "detail": detail_message,
            }
        )
        return Response(serializer.data)

    @extend_schema(
        request=None,
        responses={200: EnqueueEmbeddingResponseSerializer},
    )
    @action(detail=True, methods=["post"], url_path="enqueue-embedding")
    def enqueue_embedding(self, request, pk=None):
        """Simulate enqueueing embedding batches without dispatching workers."""

        plan = self._get_plan(pk)
        result = enqueue_embedding_plan_sync(plan.id, dispatch_batches=False)
        plan.refresh_from_db()
        response_serializer = EnqueueEmbeddingResponseSerializer(
            {
                "plan_id": str(plan.plan_id),
                "status": plan.status,
                "chunk_ids": result.get("chunk_ids", []),
                "batches": result.get("batches", []),
                "detail": result.get("detail", ""),
            }
        )
        return Response(response_serializer.data)

    @extend_schema(
        request=EmbeddingBatchRequestSerializer,
        responses={
            200: EmbeddingPreviewResponseSerializer,
            400: OpenApiResponse(description="Embedding preview failed"),
        },
    )
    @action(detail=True, methods=["post"], url_path="embedding-preview")
    def embedding_preview(self, request, pk=None):
        """Generate embedding vectors without writing to Qdrant."""

        plan = self._get_plan(pk)
        payload = EmbeddingBatchRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        chunk_ids = data.get("chunk_ids") or list(
            plan.chunks.filter(
                embedding_status__in=[ChunkEmbeddingStatus.PENDING, ChunkEmbeddingStatus.FAILED],
            )
            .order_by("sequence")
            .values_list("id", flat=True)
        )

        if not chunk_ids:
            return Response(
                {"detail": "No chunks available for preview"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = embed_chunk_batch_sync(
            plan.id,
            chunk_ids,
            persist=False,
            check_completion=False,
        )
        if result.get("status") == "failed":
            return Response(
                {"detail": result.get("detail", "Embedding preview failed")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        chunks = list(
            plan.chunks.filter(id__in=chunk_ids).order_by("sequence")
        )
        vector_data = result.get("vectors", [])
        previews: list[dict[str, object]] = []
        for chunk, vector in zip(chunks, vector_data):
            preview = vector[: min(len(vector), 8)] if vector else []
            previews.append(
                {
                    "chunk_id": chunk.id,
                    "sequence": chunk.sequence,
                    "token_count": chunk.token_count,
                    "vector_dimensions": len(vector),
                    "vector_preview": preview,
                }
            )

        response_serializer = EmbeddingPreviewResponseSerializer(
            {
                "plan_id": str(plan.plan_id),
                "embedded": result.get("embedded", 0),
                "chunks": previews,
            }
        )
        return Response(response_serializer.data)

    @extend_schema(
        request=EmbeddingBatchRequestSerializer,
        responses={
            200: EmbeddingExecuteResponseSerializer,
            400: OpenApiResponse(description="Embedding execution failed"),
        },
    )
    @action(detail=True, methods=["post"], url_path="embedding-execute")
    def embedding_execute(self, request, pk=None):
        """Run embedding for selected chunks and persist results."""

        plan = self._get_plan(pk)
        payload = EmbeddingBatchRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        chunk_ids = data.get("chunk_ids") or list(
            plan.chunks.filter(
                embedding_status__in=[
                    ChunkEmbeddingStatus.PENDING,
                    ChunkEmbeddingStatus.FAILED,
                    ChunkEmbeddingStatus.QUEUED,
                ],
            )
            .order_by("sequence")
            .values_list("id", flat=True)
        )

        if not chunk_ids:
            return Response(
                {"detail": "No chunks available for embedding"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = embed_chunk_batch_sync(
            plan.id,
            chunk_ids,
            persist=True,
            check_completion=True,
        )

        if result.get("status") == "failed":
            return Response(
                {"detail": result.get("detail", "Embedding failed")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        plan.refresh_from_db()
        response_serializer = EmbeddingExecuteResponseSerializer(
            {
                "plan_id": str(plan.plan_id),
                "status": plan.status,
                "embedded": result.get("embedded", 0),
            }
        )
        return Response(response_serializer.data)

    @extend_schema(
        request=None,
        responses={200: PlanStatusResponseSerializer},
    )
    @action(detail=True, methods=["get"], url_path="status")
    def status(self, request, pk=None):
        """Return a snapshot of plan progress and outstanding work."""

        plan = self._get_plan(pk)
        _check_plan_completion(plan.id)
        plan.refresh_from_db()

        total = plan.chunks.count()
        embedded = plan.chunks.filter(embedding_status=ChunkEmbeddingStatus.EMBEDDED).count()
        failed = plan.chunks.filter(embedding_status=ChunkEmbeddingStatus.FAILED).count()

        response_serializer = PlanStatusResponseSerializer(
            {
                "plan_id": str(plan.plan_id),
                "status": plan.status,
                "bundle_count": plan.bundle_count,
                "chunk_count": plan.chunk_count,
                "embedded_chunks": embedded,
                "failed_chunks": failed,
                "pending_chunks": total - embedded - failed,
                "last_error": plan.last_error,
                "is_active": plan.is_active,
            }
        )
        return Response(response_serializer.data)

    @extend_schema(
        request=None,
        responses={200: PlanResetResponseSerializer, 404: OpenApiResponse(description="Plan not found")},
    )
    @action(detail=True, methods=["post"], url_path="reset")
    def reset(self, request, pk=None):
        """Reset plan state and clear stored bundles/chunks."""

        plan = reset_plan_state(pk)
        if plan is None:
            return Response({"detail": "plan not found"}, status=status.HTTP_404_NOT_FOUND)

        bundle_deleted = plan.bundles.count()
        chunk_deleted = plan.chunks.count()
        plan.bundles.all().delete()
        plan.chunks.all().delete()

        plan.refresh_from_db()
        response_serializer = PlanResetResponseSerializer(
            {
                "plan_id": str(plan.plan_id),
                "status": plan.status,
                "bundle_deleted": bundle_deleted,
                "chunk_deleted": chunk_deleted,
            }
        )
        return Response(response_serializer.data)


class QdrantSearchViewSet(viewsets.ViewSet):
    """Execute text similarity searches across one or more IndexProfiles."""

    http_method_names = ["post", "head", "options"]

    @extend_schema(
        request=QdrantAutoSearchRequestSerializer,
        responses={200: QdrantAutoSearchResponseSerializer},
    )
    def create(self, request):
        payload = QdrantAutoSearchRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        text = data["text"].strip()
        if not text:
            raise ValidationError({"text": "Query text must not be empty."})

        profile_ids = data.get("profile_ids")
        profiles: list[IndexProfile]
        if profile_ids:
            profiles = list(
                IndexProfile.objects.filter(id__in=profile_ids, is_active=True)
            )
            missing_ids = [pid for pid in profile_ids if pid not in {p.id for p in profiles}]
            if missing_ids:
                raise ValidationError({"profile_ids": f"Profiles not found or inactive: {missing_ids}"})
            profile_map = {profile.id: profile for profile in profiles}
            profiles = [profile_map[profile_id] for profile_id in profile_ids if profile_id in profile_map]
        else:
            profile_limit = data.get("profile_limit") or 1
            profiles = list(
                IndexProfile.objects.filter(is_active=True)
                .order_by("-updated_at")[:profile_limit]
            )

        if not profiles:
            return Response(
                {"detail": "No active profiles available for search."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        limit = data.get("limit") or 10
        score_threshold = data.get("score_threshold")
        with_payload = data.get("with_payload", True)
        with_vector_preview = data.get("with_vector_preview", False)

        overall_tokens = 0
        warnings: list[str] = []
        results: list[dict[str, object]] = []
        overall_start = time.perf_counter()

        for profile in profiles:
            profile_start = time.perf_counter()
            embedding_time_ms: float | None = None
            search_time_ms: float | None = None
            db_time_ms: float | None = None

            try:
                tokenizer = get_tokenizer(profile.tokenizer)
            except Exception as exc:  # noqa: BLE001
                warning = f"Tokenizer load failed for profile {profile.slug}: {exc}"
                logger.warning(warning)
                warnings.append(warning)
                continue

            tokens = tokenizer.encode(text, add_special_tokens=False)
            token_count = len(tokens)
            overall_tokens = max(overall_tokens, token_count)

            if profile.max_input_tokens and token_count > profile.max_input_tokens:
                warning = (
                    f"Query tokens ({token_count}) exceed encoder window "
                    f"({profile.max_input_tokens}) for profile {profile.slug}; "
                    "text will be truncated by the tokenizer."
                )
                logger.warning(warning)
                warnings.append(warning)

            try:
                embed_start = time.perf_counter()
                embeddings = embed_texts(profile.encoder, [text])
                embedding_time_ms = (time.perf_counter() - embed_start) * 1000.0
            except Exception as exc:  # noqa: BLE001
                warning = f"Embedding failed for profile {profile.slug}: {exc}"
                logger.warning(warning)
                warnings.append(warning)
                continue

            if not embeddings:
                logger.info("Embedding produced no vectors for profile %s", profile.slug)
                results.append(
                    {
                        "profile_id": profile.id,
                        "profile_slug": profile.slug,
                        "collection": profile.qdrant_collection,
                        "used_tokens": token_count,
                        "returned": 0,
                        "matches": [],
                    }
                )
                continue

            query_vector = embeddings[0]

            try:
                search_start = time.perf_counter()
                q_results = search_collection(
                    profile,
                    query_vector,
                    limit=limit,
                    score_threshold=score_threshold,
                    with_payload=with_payload,
                    with_vectors=with_vector_preview,
                )
                search_time_ms = (time.perf_counter() - search_start) * 1000.0
            except Exception as exc:  # noqa: BLE001
                warning = f"Qdrant search failed for profile {profile.slug}: {exc}"
                logger.warning(warning)
                warnings.append(warning)
                continue

            matches: list[dict[str, object]] = []
            db_fetch_start = time.perf_counter()
            for record in q_results or []:
                payload_data = getattr(record, "payload", {}) or {}
                chunk_id = payload_data.get("chunk_pk") or payload_data.get("chunk_id")
                try:
                    chunk_id = int(chunk_id) if chunk_id is not None else None
                except (TypeError, ValueError):
                    pass

                plan_value = payload_data.get("plan_id")
                if plan_value is not None:
                    plan_value = str(plan_value)

                vector_preview: list[float] = []
                if with_vector_preview:
                    vector_data = getattr(record, "vector", None)
                    if vector_data:
                        vector_preview = list(vector_data)[:8]

                matches.append(
                    {
                        "id": str(getattr(record, "id", "")),
                        "score": float(getattr(record, "score", 0.0)),
                        "chunk_id": chunk_id,
                        "plan_id": plan_value,
                        "chunk_sequence": payload_data.get("chunk_sequence"),
                        "bundle_sequence": payload_data.get("bundle_sequence"),
                        "token_count": payload_data.get("token_count"),
                        "paper_id": payload_data.get("paper_id"),
                        "payload": payload_data if with_payload else {},
                        "vector_preview": vector_preview if with_vector_preview else [],
                    }
                )

            chunk_ids_to_load = [m["chunk_id"] for m in matches if m.get("chunk_id")]
            chunk_lookup: dict[int, Chunk] = {}
            if chunk_ids_to_load:
                chunk_lookup = {
                    chunk.id: chunk
                    for chunk in Chunk.objects.filter(id__in=chunk_ids_to_load)
                }
            db_time_ms = (time.perf_counter() - db_fetch_start) * 1000.0 if chunk_ids_to_load else 0.0

            for match in matches:
                chunk_text = ""
                chunk_id = match.get("chunk_id")
                if chunk_id and chunk_id in chunk_lookup:
                    chunk_text = chunk_lookup[chunk_id].text or ""
                elif chunk_id:
                    logger.warning(
                        "Chunk text unavailable: chunk %s not found during auto-search (profile=%s)",
                        chunk_id,
                        profile.slug,
                    )
                match["chunk_text"] = chunk_text

            results.append(
                {
                    "profile_id": profile.id,
                    "profile_slug": profile.slug,
                    "collection": profile.qdrant_collection,
                    "used_tokens": token_count,
                    "returned": len(matches),
                    "matches": matches,
                    "embedding_time_ms": embedding_time_ms,
                    "search_time_ms": search_time_ms,
                    "db_time_ms": db_time_ms,
                }
            )

            logger.info(
                "Auto search profile=%s collection=%s query_tokens=%s returned=%s limit=%s",
                profile.slug,
                profile.qdrant_collection,
                token_count,
                len(matches),
                limit,
            )

        if not results:
            return Response(
                {
                    "detail": "Search skipped; all profiles failed.",
                    "warnings": warnings,
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        overall_elapsed_ms = (time.perf_counter() - overall_start) * 1000.0

        response_serializer = QdrantAutoSearchResponseSerializer(
            {
                "query_tokens": overall_tokens,
                "limit": limit,
                "score_threshold": score_threshold,
                "profiles_checked": results,
                "warnings": warnings,
                "elapsed_ms": overall_elapsed_ms,
            }
        )
        return Response(response_serializer.data)
