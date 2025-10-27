"""Admin registrations for pastpaper app."""
import os

from django.contrib import admin
from django.db.models import Count
from django.utils.text import Truncator

from .models import (
    PastPaper,
    PastPaperAsset,
    PastPaperComponent,
    PastPaperMetadata,
)


class BasePastPaperInline(admin.TabularInline):
    model = PastPaper
    extra = 0
    fields = (
        "paper_id",
        "version_no",
        "parsed_state",
        "is_active",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields
    show_change_link = True
    ordering = ("-version_no",)
    can_delete = False


class PastPaperByMetadataInline(BasePastPaperInline):
    fk_name = "metadata"


class PastPaperByAssetInline(BasePastPaperInline):
    fk_name = "asset"


@admin.register(PastPaperMetadata)
class PastPaperMetadataAdmin(admin.ModelAdmin):
    list_display = (
        "paper_code",
        "exam_board",
        "subject",
        "syllabus_code",
        "season",
        "year",
        "variant_no",
        "paper_type",
        "paper_count",
        "created_at",
        "updated_at",
    )
    search_fields = (
        "paper_code",
        "exam_board",
        "subject",
        "syllabus_code",
    )
    list_filter = (
        "exam_board",
        "subject",
        "season",
        "year",
        "paper_type",
        "variant_no",
    )
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"
    inlines = [PastPaperByMetadataInline]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(_paper_count=Count("past_papers", distinct=True))

    @admin.display(ordering="_paper_count", description="Past papers")
    def paper_count(self, obj):
        return getattr(obj, "_paper_count", 0)


@admin.register(PastPaperAsset)
class PastPaperAssetAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "file_name",
        "mime",
        "pages",
        "size",
        "checksum_preview",
        "created_at",
    )
    search_fields = ("file", "checksum_sha256")
    list_filter = ("mime",)
    ordering = ("-created_at",)
    readonly_fields = (
        "size",
        "checksum_sha256",
        "created_at",
    )
    date_hierarchy = "created_at"
    inlines = [PastPaperByAssetInline]

    @admin.display(description="File")
    def file_name(self, obj):
        return os.path.basename(obj.file.name) if obj.file else ""

    @admin.display(description="Checksum")
    def checksum_preview(self, obj):
        return obj.checksum_sha256[:8] + "..." if obj.checksum_sha256 else ""


@admin.register(PastPaper)
class PastPaperAdmin(admin.ModelAdmin):
    list_display = (
        "paper_id",
        "version_no",
        "metadata",
        "exam_board",
        "subject",
        "paper_type",
        "parsed_state",
        "is_active",
        "created_at",
        "updated_at",
    )
    search_fields = (
        "metadata__paper_code",
        "metadata__subject",
        "metadata__syllabus_code",
        "asset__checksum_sha256",
    )
    list_filter = (
        "parsed_state",
        "is_active",
        "metadata__exam_board",
        "metadata__subject",
        "metadata__season",
        "metadata__year",
        "metadata__paper_type",
    )
    list_select_related = ("metadata", "asset")
    ordering = ("-created_at",)
    raw_id_fields = ("metadata", "asset")
    readonly_fields = ("paper_id", "version_no", "created_at", "updated_at")
    date_hierarchy = "created_at"

    @admin.display(ordering="metadata__exam_board", description="Exam board")
    def exam_board(self, obj):
        return obj.metadata.exam_board

    @admin.display(ordering="metadata__subject", description="Subject")
    def subject(self, obj):
        return obj.metadata.subject

    @admin.display(ordering="metadata__paper_type", description="Paper type")
    def paper_type(self, obj):
        return obj.metadata.paper_type


@admin.register(PastPaperComponent)
class PastPaperComponentAdmin(admin.ModelAdmin):
    list_display = (
        "paper",
        "path_normalized",
        "num_display",
        "depth",
        "score",
        "page",
        "content_preview",
        "created_at",
    )
    search_fields = (
        "path_normalized",
        "num_display",
        "content",
    )
    list_filter = ("depth",)
    list_select_related = ("paper", "paper__metadata")
    ordering = ("paper", "path_normalized")
    raw_id_fields = ("paper", "parent")
    readonly_fields = (
        "path_normalized",
        "depth",
        "created_at",
        "updated_at",
    )

    @admin.display(description="Content")
    def content_preview(self, obj):
        return Truncator(obj.content).chars(75) if obj.content else ""
