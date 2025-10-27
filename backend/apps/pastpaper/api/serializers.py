from typing import Any, Dict, Optional
from django.db import IntegrityError, transaction
from rest_framework import serializers

from backend.apps.pastpaper.models import PastPaper, PastPaperAsset, PastPaperMetadata
from backend.apps.pastpaper.utils import compute_sha256_and_size, infer_pdf_pages
from backend.apps.pastpaper.tasks import trigger_parse_async


class PastPaperMetadataSerializer(serializers.ModelSerializer):
    class Meta:
        model = PastPaperMetadata
        fields = [
            "id",
            "paper_code",
            "exam_board",
            "subject",
            "syllabus_code",
            "season",
            "year",
            "variant_no",
            "paper_type",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class PastPaperAssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = PastPaperAsset
        fields = [
            "id",
            "file",
            "mime",
            "size",
            "checksum_sha256",
            "pages",
            "url_source",
            "created_at",
        ]
        read_only_fields = ["id", "size", "checksum_sha256", "pages", "created_at"]


class PastPaperSerializer(serializers.ModelSerializer):
    metadata = PastPaperMetadataSerializer(read_only=True)
    asset = PastPaperAssetSerializer(read_only=True)

    class Meta:
        model = PastPaper
        fields = [
            "paper_id",
            "version_no",
            "parsed_state",
            "last_error",
            "is_active",
            "created_at",
            "updated_at",
            "parsed_tree",
            "metadata",
            "asset",
        ]
        read_only_fields = fields


# ---------- Create (POST /pastpaper) ----------
class PastPaperCreateSerializer(serializers.Serializer):
    # metadata
    paper_code = serializers.CharField(max_length=255)
    exam_board = serializers.CharField(max_length=64)
    subject = serializers.CharField(max_length=128)
    year = serializers.IntegerField()
    syllabus_code = serializers.CharField(required=False, allow_blank=True, default="")
    season = serializers.CharField(required=False, allow_blank=True, default="")
    variant_no = serializers.CharField(required=False, allow_blank=True, default="")
    paper_type = serializers.CharField(required=False, allow_blank=True, default="")

    # asset
    file = serializers.FileField()
    mime = serializers.CharField(required=False, allow_blank=True, default="application/pdf")
    url_source = serializers.CharField(required=False, allow_blank=True, default="")
    use_image = serializers.BooleanField(required=False, default=False)

    def validate_paper_code(self, value: str) -> str:
        if PastPaperMetadata.objects.filter(paper_code=value).exists():
            raise serializers.ValidationError("paper_code already exists")
        return value

    def _get_or_create_metadata(self, data: Dict[str, Any]) -> PastPaperMetadata:
        code = data["paper_code"]
        try:
            return PastPaperMetadata.objects.create(
                paper_code=code,
                exam_board=data["exam_board"],
                subject=data["subject"],
                syllabus_code=data.get("syllabus_code", ""),
                season=data.get("season", ""),
                year=data["year"],
                variant_no=data.get("variant_no", ""),
                paper_type=data.get("paper_type", ""),
            )
        except IntegrityError:
            raise serializers.ValidationError({"paper_code": "paper_code already exists"})

    def _create_asset(self, file, mime: str, url_source: str) -> PastPaperAsset:
        checksum, size = compute_sha256_and_size(file)
        pages = infer_pdf_pages(file) if (mime or "").lower() == "application/pdf" else None
        asset = PastPaperAsset.objects.create(
            file=file,
            mime=mime or "application/pdf",
            size=size,
            checksum_sha256=checksum,
            pages=pages,
            url_source=url_source or "",
        )
        return asset

    @transaction.atomic
    def create(self, validated_data):
        meta = self._get_or_create_metadata(validated_data)
        asset = self._create_asset(
            validated_data["file"], validated_data.get("mime"), validated_data.get("url_source", "")
        )
        paper = PastPaper.objects.create(
            metadata=meta,
            asset=asset,
            # paper_id 自动生成；version_no 在 model.save() 自动分配
            is_active=True,
        )
        # 设置其他版本 is_active=False（理论上第一版没有其他版本）
        PastPaper.objects.filter(paper_id=paper.paper_id).exclude(pk=paper.pk).update(is_active=False)

        # 触发解析任务（异步）
        trigger_parse_async(
            paper_id=str(paper.paper_id),
            version_no=paper.version_no,
            use_image=validated_data.get("use_image", False)
        )

        return paper

    def to_representation(self, instance: PastPaper):
        return PastPaperSerializer(instance).data


class PastPaperAppendSerializer(serializers.Serializer):
    paper_id = serializers.UUIDField()

    file = serializers.FileField(required=False)
    mime = serializers.CharField(required=False, allow_blank=True, default="application/pdf")
    url_source = serializers.CharField(required=False, allow_blank=True, default="")

    metadata = PastPaperMetadataSerializer(required=False)

    def _pick_baseline(self, paper_id) -> PastPaper:
        base_qs = PastPaper.objects.select_for_update().filter(paper_id=paper_id, is_active=True).order_by(
            "-version_no")
        base = base_qs.first()
        if not base:
            base = PastPaper.objects.select_for_update().filter(paper_id=paper_id).order_by("-version_no").first()
        if not base:
            raise serializers.ValidationError("paper_id 不存在任何版本")
        return base

    def _create_or_reuse_metadata(self, meta_payload: Optional[Dict[str, Any]], fallback: PastPaperMetadata):
        if not meta_payload:
            return fallback
        code = meta_payload.get("paper_code")
        if not code:
            raise serializers.ValidationError("metadata.paper_code 不能为空")
        # paper_code 唯一：若已存在则复用，不覆盖字段
        exists = PastPaperMetadata.objects.filter(paper_code=code).first()
        if exists:
            return exists
        return PastPaperMetadata.objects.create(**meta_payload)

    def _maybe_create_asset(self, file, mime: str, url_source: str, fallback: PastPaperAsset) -> PastPaperAsset:
        if not file:
            return fallback
        checksum, size = compute_sha256_and_size(file)
        pages = infer_pdf_pages(file) if (mime or "").lower() == "application/pdf" else None
        return PastPaperAsset.objects.create(
            file=file,
            mime=mime or "application/pdf",
            size=size,
            checksum_sha256=checksum,
            pages=pages,
            url_source=url_source or "",
        )

    @transaction.atomic
    def create(self, validated_data):
        paper_id = validated_data["paper_id"]
        base = self._pick_baseline(paper_id)
        meta_payload = validated_data.get("metadata")
        file = validated_data.get("file")
        mime = validated_data.get("mime", "application/pdf")
        url_source = validated_data.get("url_source", "")

        # 1. 处理 metadata
        metadata = base.metadata
        if meta_payload:
            if "paper_code" in meta_payload and meta_payload["paper_code"] != metadata.paper_code:
                raise serializers.ValidationError("不允许修改 paper_code，请新建逻辑卷")
            # 更新 metadata 其他字段
            for field, value in meta_payload.items():
                if field != "paper_code" and hasattr(metadata, field):
                    setattr(metadata, field, value)
            metadata.save(update_fields=[f for f in meta_payload.keys() if f != "paper_code"])
            # 重置所有 PastPaper 状态并触发解析
            papers = PastPaper.objects.select_for_update().filter(paper_id=paper_id)
            papers.update(parsed_state="PENDING", last_error="", parsed_tree=None)
            for p in PastPaper.objects.filter(paper_id=paper_id):
                trigger_parse_async(paper_id=str(p.paper_id), version_no=p.version_no)

        # 2. 处理 asset
        if file:
            checksum, size = compute_sha256_and_size(file)
            pages = infer_pdf_pages(file) if mime.lower() == "application/pdf" else None
            new_asset = PastPaperAsset.objects.create(
                file=file,
                mime=mime,
                size=size,
                checksum_sha256=checksum,
                pages=pages,
                url_source=url_source or "",
            )
            new_paper = PastPaper.objects.create(
                paper_id=paper_id,
                metadata=metadata,
                asset=new_asset,
                is_active=True,
                parsed_state="PENDING",
                last_error="",
                parsed_tree=None,
            )
            PastPaper.objects.filter(paper_id=paper_id).exclude(pk=new_paper.pk).update(is_active=False)
            trigger_parse_async(paper_id=str(new_paper.paper_id), version_no=new_paper.version_no)
            return new_paper

        # 如果只更新了 metadata 没有新文件，就返回最新版本
        return PastPaper.objects.filter(paper_id=paper_id).order_by("-version_no").first()

    def to_representation(self, instance: PastPaper):
        return PastPaperSerializer(instance).data


# ---------- System state update (PATCH /pastpaper/state) ----------
class PastPaperStateUpdateSerializer(serializers.Serializer):
    paper_id = serializers.UUIDField()
    version_no = serializers.IntegerField(required=False)  # 默认选择最新
    parsed_state = serializers.ChoiceField(choices=["PENDING", "RUNNING", "READY", "ERROR"], required=False)
    last_error = serializers.CharField(required=False, allow_blank=True)
    parsed_tree = serializers.JSONField(required=False)

    @transaction.atomic
    def update(self, instance, validated_data):
        # 不使用 instance；按 paper_id + version_no 自行获取
        paper_id = validated_data["paper_id"]
        version_no = validated_data.get("version_no")
        qs = PastPaper.objects.filter(paper_id=paper_id).order_by("-version_no")
        paper = qs.first() if version_no is None else qs.filter(version_no=version_no).first()
        if not paper:
            raise serializers.ValidationError("指定的 PastPaper 版本不存在")

        if "parsed_state" in validated_data:
            paper.parsed_state = validated_data["parsed_state"]
        if "last_error" in validated_data:
            paper.last_error = validated_data["last_error"]
        if "parsed_tree" in validated_data:
            paper.parsed_tree = validated_data["parsed_tree"]
        paper.save(update_fields=["parsed_state", "last_error", "parsed_tree", "updated_at"])
        return paper

    def to_representation(self, instance: PastPaper):
        return {
            "paper_id": str(instance.paper_id),
            "version_no": instance.version_no,
            "parsed_state": instance.parsed_state,
            "last_error": instance.last_error,
            "updated_at": instance.updated_at,
        }


class PastPaperListQuerySerializer(serializers.Serializer):
    paper_id = serializers.UUIDField(required=False)
    paper_code = serializers.CharField(required=False)
    type = serializers.ChoiceField(choices=["asset", "pdf", "parsed"], required=False)
    active_only = serializers.BooleanField(required=False)
    redirect = serializers.BooleanField(required=False)
    version_no = serializers.IntegerField(required=False)


class PastPaperRetrieveQuerySerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=["asset", "pdf", "parsed"], required=False)
    redirect = serializers.BooleanField(required=False)
    active_only = serializers.BooleanField(required=False)
    version_no = serializers.IntegerField(required=False)


class PastPaperPDFResponseSerializer(serializers.Serializer):
    paper_id = serializers.UUIDField()
    version_no = serializers.IntegerField()
    pdf_url = serializers.URLField(allow_blank=True)


class PastPaperParsedResponseSerializer(serializers.Serializer):
    paper_id = serializers.UUIDField()
    version_no = serializers.IntegerField()
    parsed_state = serializers.CharField()
    parsed_tree = serializers.JSONField(allow_null=True)


class PastPaperStateUpdateResponseSerializer(serializers.Serializer):
    paper_id = serializers.UUIDField()
    version_no = serializers.IntegerField()
    parsed_state = serializers.CharField()
    last_error = serializers.CharField(allow_blank=True)
    updated_at = serializers.DateTimeField()

    def to_representation(self, instance):
        if isinstance(instance, PastPaper):
            instance = {
                "paper_id": instance.paper_id,
                "version_no": instance.version_no,
                "parsed_state": instance.parsed_state,
                "last_error": instance.last_error,
                "updated_at": instance.updated_at,
            }
        return super().to_representation(instance)


class PastPaperListByCodeResponseSerializer(serializers.Serializer):
    paper_code = serializers.CharField()
    count = serializers.IntegerField()
    results = PastPaperSerializer(many=True)


class PastPaperDeleteRequestSerializer(serializers.Serializer):
    paper_id = serializers.UUIDField()
    version_no = serializers.IntegerField(required=False)


class PastPaperDeleteResponseSerializer(serializers.Serializer):
    deleted = serializers.BooleanField()
    paper_id = serializers.UUIDField()
    version_no = serializers.CharField()


class PastPaperComponentsQuerySerializer(serializers.Serializer):
    paper_id = serializers.UUIDField()
    version_no = serializers.IntegerField(required=False)
    flat = serializers.BooleanField(required=False)
    path_prefix = serializers.CharField(required=False)


class PastPaperComponentFlatSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    num_display = serializers.CharField(allow_blank=True)
    path_normalized = serializers.CharField()
    depth = serializers.IntegerField()
    page = serializers.IntegerField(allow_null=True)
    score = serializers.FloatField(allow_null=True)
    content = serializers.CharField(allow_blank=True, allow_null=True)
    position = serializers.JSONField(allow_null=True)


class PastPaperComponentTreeNodeSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    num_display = serializers.CharField(allow_blank=True)
    path_normalized = serializers.CharField()
    depth = serializers.IntegerField()
    page = serializers.IntegerField(allow_null=True)
    score = serializers.FloatField(allow_null=True)
    content = serializers.CharField(allow_blank=True, allow_null=True)
    position = serializers.JSONField(allow_null=True)
    children = serializers.ListField(child=serializers.JSONField(), required=False)


class PastPaperComponentsResponseSerializer(serializers.Serializer):
    paper_id = serializers.UUIDField()
    version_no = serializers.IntegerField()
    count = serializers.IntegerField(required=False)
    results = PastPaperComponentFlatSerializer(many=True, required=False)
    components = PastPaperComponentTreeNodeSerializer(many=True, required=False)


class PastPaperComponentSearchQuerySerializer(serializers.Serializer):
    keyword = serializers.CharField()
    limit = serializers.IntegerField(required=False, min_value=1, max_value=100, default=20)
    offset = serializers.IntegerField(required=False, min_value=0, default=0)
    paper_id = serializers.UUIDField(required=False)
    fuzzy = serializers.BooleanField(required=False, default=True)


class PastPaperComponentSearchResultSerializer(serializers.Serializer):
    component_id = serializers.IntegerField()
    paper_pk = serializers.IntegerField()
    paper_id = serializers.UUIDField()
    version_no = serializers.IntegerField()
    num_display = serializers.CharField(allow_blank=True)
    path_normalized = serializers.CharField()
    depth = serializers.IntegerField()
    page = serializers.IntegerField(allow_null=True)
    content = serializers.CharField(allow_blank=True, allow_null=True)
    similarity = serializers.FloatField()


class PastPaperComponentSearchResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = PastPaperComponentSearchResultSerializer(many=True)


class PastPaperParseRequestSerializer(serializers.Serializer):
    paper_id = serializers.UUIDField()
    version_no = serializers.IntegerField(required=False)
    use_image = serializers.BooleanField(required=False)


class PastPaperParseResponseSerializer(serializers.Serializer):
    enqueued = serializers.BooleanField()
    paper_id = serializers.UUIDField()
    version_no = serializers.IntegerField()
    parsed_state = serializers.CharField()


class PastPaperReparseErrorsRequestSerializer(serializers.Serializer):
    limit = serializers.IntegerField(required=False, default=-1)
    use_image = serializers.BooleanField(required=False)


class PastPaperReparseErrorsResponsePaperSerializer(serializers.Serializer):
    paper_id = serializers.UUIDField()
    version_no = serializers.IntegerField()


class PastPaperReparseErrorsResponseSerializer(serializers.Serializer):
    enqueued = serializers.BooleanField()
    count = serializers.IntegerField()
    papers = PastPaperReparseErrorsResponsePaperSerializer(many=True)


class PastPaperTestTaskRequestSerializer(serializers.Serializer):
    payload = serializers.CharField(required=False, allow_blank=True, default="ping")


class PastPaperTestTaskResponseSerializer(serializers.Serializer):
    enqueued = serializers.BooleanField()
    message_id = serializers.CharField()
    payload = serializers.CharField(allow_blank=True)
