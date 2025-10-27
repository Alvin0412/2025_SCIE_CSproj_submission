"""DRF serializers for indexing models."""

from rest_framework import serializers

from ..models import Bundle, Chunk, ChunkPlan, IndexProfile
from backend.apps.pastpaper.models import PastPaper


class IndexProfileSerializer(serializers.ModelSerializer):
    """Full serializer for index profile configurations."""

    class Meta:
        model = IndexProfile
        fields = [
            "id",
            "slug",
            "display_name",
            "description",
            "encoder",
            "tokenizer",
            "dimension",
            "max_input_tokens",
            "chunk_size",
            "chunk_overlap",
            "target_bundle_tokens",
            "qdrant_collection",
            "qdrant_distance",
            "hnsw_m",
            "hnsw_ef_construct",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ("id", "created_at", "updated_at")


class ChunkPlanSerializer(serializers.ModelSerializer):
    """Expose chunk plan metadata for CRUD operations."""

    class Meta:
        model = ChunkPlan
        fields = [
            "id",
            "plan_id",
            "paper",
            "profile",
            "status",
            "last_error",
            "bundle_count",
            "chunk_count",
            "is_active",
            "created_at",
            "updated_at",
            "bundled_at",
            "embedded_at",
        ]
        read_only_fields = (
            "id",
            "plan_id",
            "bundle_count",
            "chunk_count",
            "created_at",
            "updated_at",
            "bundled_at",
            "embedded_at",
        )


class BundleSerializer(serializers.ModelSerializer):
    """Persisted bundle representation."""

    class Meta:
        model = Bundle
        fields = [
            "id",
            "plan",
            "root_component",
            "sequence",
            "title",
            "component_ids",
            "span_paths",
            "text",
            "token_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ("id", "created_at", "updated_at")


class ChunkSerializer(serializers.ModelSerializer):
    """Persisted chunk representation."""

    class Meta:
        model = Chunk
        fields = [
            "id",
            "plan",
            "bundle",
            "sequence",
            "text",
            "token_count",
            "char_start",
            "char_end",
            "embedding_status",
            "qdrant_point_id",
            "embedded_at",
            "last_error",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ("id", "created_at", "updated_at")


class ChunkPlanCreateSerializer(serializers.Serializer):
    """Serializer dedicated to creating chunk plans with required references."""

    paper = serializers.PrimaryKeyRelatedField(queryset=PastPaper.objects.all())
    profile = serializers.PrimaryKeyRelatedField(queryset=IndexProfile.objects.all())

    def validate(self, attrs):
        paper = attrs["paper"]
        profile = attrs["profile"]
        if ChunkPlan.objects.filter(paper=paper, profile=profile).exists():
            raise serializers.ValidationError(
                "A chunk plan already exists for this paper/profile combination."
            )
        return attrs

    def create(self, validated_data):
        return ChunkPlan.objects.create(
            paper=validated_data["paper"],
            profile=validated_data["profile"],
        )
