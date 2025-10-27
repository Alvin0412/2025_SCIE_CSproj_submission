import uuid
from django.db import models
from django.contrib.postgres.indexes import GinIndex
from django.utils import timezone


class PastPaperMetadata(models.Model):
    paper_code = models.CharField(max_length=255, unique=True)  # e.g. 9489_w22_ms_32
    exam_board = models.CharField(max_length=64)  # e.g. CAIE, Edexcel, CollegeBoard
    subject = models.CharField(max_length=128)  # e.g. IGCSE Math
    syllabus_code = models.CharField(max_length=64, blank=True, default="")  # e.g. 0640
    season = models.CharField(max_length=64, blank=True, default="")  # e.g. March, May/Jun
    year = models.IntegerField()
    variant_no = models.CharField(max_length=16, blank=True, default="")
    paper_type = models.CharField(max_length=16, blank=True, default="")  # e.g. ms, qp, cr, etc.
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["exam_board", "subject", "syllabus_code"]),
            models.Index(fields=["year", "paper_type", "variant_no"]),
            models.Index(fields=["paper_code"]),
        ]

    def __str__(self):
        return (
            f"PastPaperMetadata(code={self.paper_code}, board={self.exam_board}, "
            f"subject={self.subject}, year={self.year}, type={self.paper_type}, "
            f"variant={self.variant_no or 'N/A'})"
        )


class PastPaperAsset(models.Model):
    file = models.FileField(upload_to="pastpapers/%Y/%m/%d/")
    mime = models.CharField(max_length=64, blank=True, default="application/pdf")
    size = models.BigIntegerField(default=0)
    checksum_sha256 = models.CharField(max_length=64)  # 文件内容哈希
    pages = models.IntegerField(null=True, blank=True)
    url_source = models.CharField(max_length=256, blank=True)  # 原始来源（可选）
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["checksum_sha256"]),
        ]

    def __str__(self):
        return (
            f"PastPaperAsset(id={self.id}, sha={self.checksum_sha256[:8]}..., "
            f"size={self.size}B, pages={self.pages or 'N/A'})"
        )


class PastPaper(models.Model):
    paper_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True) # 所有版本共享同一个 paper_id

    metadata = models.ForeignKey(PastPaperMetadata, on_delete=models.CASCADE, related_name="past_papers")
    asset = models.ForeignKey(PastPaperAsset, on_delete=models.PROTECT, related_name="past_papers")

    version_no = models.PositiveIntegerField(editable=False)

    parsed_state = models.CharField(
        max_length=16,
        choices=[
            ("PENDING", "PENDING"),
            ("RUNNING", "RUNNING"),
            ("READY", "READY"),
            ("ERROR", "ERROR"),
        ],
        default="PENDING",
    )
    last_error = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    parsed_tree = models.JSONField(null=True, blank=True)

    class Meta:
        unique_together = [("paper_id", "version_no")]
        indexes = [
            models.Index(fields=["paper_id"]),
            models.Index(fields=["metadata"]),
            models.Index(fields=["is_active"]),
        ]
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.version_no:  # 创建时自动分配
            last_version = (
                PastPaper.objects.filter(paper_id=self.paper_id)
                .aggregate(models.Max("version_no"))
                .get("version_no__max")
            )
            self.version_no = (last_version or 0) + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"PastPaper(paper_id={self.paper_id}, v{self.version_no}, "
            f"state={self.parsed_state}, active={self.is_active})"
        )


class PastPaperComponent(models.Model):
    paper = models.ForeignKey(PastPaper, on_delete=models.CASCADE, related_name="components")
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )

    num_display = models.CharField(max_length=64)
    path_normalized = models.CharField(max_length=128, blank=True, default="")
    depth = models.IntegerField(default=0)

    content = models.TextField(null=True, blank=True)
    score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    page = models.IntegerField(null=True, blank=True)
    position = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["paper", "num_display"]),
            models.Index(fields=["path_normalized"]),
            models.Index(fields=["depth"]),
            GinIndex(fields=["content"], name="component_content_trgm", opclasses=["gin_trgm_ops"]),
            GinIndex(fields=["num_display"], name="component_num_trgm", opclasses=["gin_trgm_ops"]),
            GinIndex(fields=["path_normalized"], name="component_path_trgm", opclasses=["gin_trgm_ops"]),
        ]

    def save(self, *args, **kwargs):
        # 自动计算 depth + path_normalized
        if self.parent:
            self.depth = self.parent.depth + 1
        else:
            self.depth = 0

        parts = []
        current = self
        while current:
            parts.append(current.num_display)
            current = current.parent
        self.path_normalized = ".".join(reversed(parts))

        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"PastPaperComponent(paper_id={self.paper_id}, path={self.path_normalized or self.num_display}, "
            f"depth={self.depth}, page={self.page or 'N/A'})"
        )
