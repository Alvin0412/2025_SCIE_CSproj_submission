"""Service app API endpoints exposed via Django Ninja."""

# backend/apps/service/api.py
from django.shortcuts import get_object_or_404
from ninja import File, NinjaAPI
from ninja.errors import HttpError
from ninja.files import UploadedFile
from sympy.printing.pytorch import torch
from sklearn.metrics.pairwise import cosine_similarity
from sympy.stats.sampling.sample_numpy import numpy

from backend.apps.service.tasks import generate_embedding
import numpy as np

from .models import TestFile
from .serializers import TestFileSchema, serialize_test_file, serialize_test_file_list

api = NinjaAPI()


@api.post("/embed/")
async def embed(request, text: str):
    result = await generate_embedding(text)
    return {"result": result}
    # return {"result": result.message_id, "status": "Task submitted"}


@api.post("/sentences_similarity/")
async def get_sentences_similarity(request, sentence1: str, sentence2: str):
    vec1 = await generate_embedding(sentence1)
    vec2 = await generate_embedding(sentence2)

    # Convert to numpy arrays and reshape to 2D (1, D)
    vec1 = np.array(vec1).reshape(1, -1)
    vec2 = np.array(vec2).reshape(1, -1)

    # Compute cosine similarity
    similarity = cosine_similarity(vec1, vec2)[0][0]

    return {
        "similarity": similarity
    }


@api.post("/files/", response={201: TestFileSchema})
def upload_test_file(request, file: UploadedFile = File(...)):
    """Store an uploaded file for testing purposes."""

    if not file:
        raise HttpError(400, "file is required")

    test_file = TestFile.objects.create(file=file)
    return 201, serialize_test_file(test_file, request)


@api.get("/files/", response=list[TestFileSchema])
def list_test_files(request):
    """Return all uploaded test files ordered by recency."""

    files = list(TestFile.objects.all())
    return serialize_test_file_list(files, request)


@api.get("/files/{file_id}", response=TestFileSchema)
def retrieve_test_file(request, file_id: int):
    """Retrieve a single uploaded test file."""

    test_file = get_object_or_404(TestFile, pk=file_id)
    return serialize_test_file(test_file, request)


@api.put("/files/{file_id}", response=TestFileSchema)
def update_test_file(request, file_id: int, file: UploadedFile = File(...)):
    """Replace the stored file contents for the given TestFile."""

    if not file:
        raise HttpError(400, "file is required")

    test_file = get_object_or_404(TestFile, pk=file_id)
    if test_file.file:
        test_file.file.delete(save=False)
    test_file.file = file
    test_file.save(update_fields=["file"])
    test_file.refresh_from_db()
    return serialize_test_file(test_file, request)


@api.delete("/files/{file_id}", response={204: None})
def delete_test_file(request, file_id: int):
    """Delete a stored TestFile and its underlying media file."""

    test_file = get_object_or_404(TestFile, pk=file_id)
    if test_file.file:
        test_file.file.delete(save=False)
    test_file.delete()
    return 204, None

# @api.get("/embed/{task_id}")
# async def get_embedding_result(request, task_id: str):
#     """
#     Retrieve the result of an embedding task by its ID.
#     """
#     return {
#         "task_id": task_id,
#         "result": get_task_result(task_id)
#     }
