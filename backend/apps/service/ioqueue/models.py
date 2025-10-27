from django.db import models
from django.utils import timezone
from django.conf import settings


class IOJob(models.Model):
    TASK_STATUS = [
        ("pending", "pending"),
        ("running", "running"),
        ("done", "done"),
        ("error", "error"),
    ]

    # 任务标识：模块.函数名
    task_name = models.CharField(max_length=255, db_index=True)
    # 参数（序列化）
    args = models.JSONField(default=list)
    kwargs = models.JSONField(default=dict)

    # 去重键（可选）
    dedupe_key = models.CharField(max_length=255, blank=True, default="", db_index=True)

    # 状态 & 可见性窗口
    status = models.CharField(max_length=16, choices=TASK_STATUS, default="pending", db_index=True)
    queued_at = models.DateTimeField(default=timezone.now, db_index=True)
    picked_at = models.DateTimeField(null=True, blank=True)
    visible_until = models.DateTimeField(null=True, blank=True)  # visibility timeout
    picked_by = models.CharField(max_length=128, blank=True, default="", db_index=True)

    # 调度/重试
    scheduled_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=3)
    last_error = models.TextField(blank=True, default="")

    # 结果（可选）
    result = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "scheduled_at"]),
            models.Index(fields=["task_name", "status"]),
        ]
