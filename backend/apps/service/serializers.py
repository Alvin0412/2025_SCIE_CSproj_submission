"""Serializers for the service app Ninja endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Schema

from .models import TestFile


class TestFileSchema(Schema):
    id: int
    filename: str
    file_url: str
    uploaded_at: datetime


def serialize_test_file(instance: TestFile, request: HttpRequest | None = None) -> TestFileSchema:
    """Render a TestFile instance into a JSON-serialisable schema."""

    if instance.file and hasattr(instance.file, "url"):
        file_url = instance.file.url
        if request is not None:
            file_url = request.build_absolute_uri(file_url)
    else:
        file_url = ""

    payload: dict[str, Any] = {
        "id": instance.id,
        "filename": instance.filename,
        "file_url": file_url,
        "uploaded_at": instance.uploaded_at,
    }
    return TestFileSchema(**payload)


def serialize_test_file_list(
    instances: list[TestFile], request: HttpRequest | None = None
) -> list[TestFileSchema]:
    """Render a list of TestFile instances."""

    return [serialize_test_file(instance, request) for instance in instances]
