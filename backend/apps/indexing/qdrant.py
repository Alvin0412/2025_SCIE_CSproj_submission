"""Helpers for interacting with Qdrant."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable
from uuid import UUID

from django.conf import settings

from qdrant_client import QdrantClient
from qdrant_client.conversions import common_types as qtypes
from qdrant_client.http import models as qmodels
from qdrant_client.http.exceptions import UnexpectedResponse

from .models import ChunkPlan, IndexProfile


DISTANCE_MAP: dict[str, qmodels.Distance] = {
    "Cosine": qmodels.Distance.COSINE,
    "COSINE": qmodels.Distance.COSINE,
    "cosine": qmodels.Distance.COSINE,
    "Dot": qmodels.Distance.DOT,
    "dot": qmodels.Distance.DOT,
    "L2": qmodels.Distance.EUCLID,
    "l2": qmodels.Distance.EUCLID,
}


@dataclass(slots=True)
class VectorRecord:
    point_id: str
    vector: list[float]
    payload: dict[str, object]


@lru_cache(maxsize=1)
def get_client() -> QdrantClient:
    return QdrantClient(
        url=getattr(settings, "QDRANT_URL", "http://localhost:6333"),
        api_key=getattr(settings, "QDRANT_API_KEY", None) or None,
        timeout=getattr(settings, "QDRANT_TIMEOUT", 20.0),
    )


def ensure_collection(profile: IndexProfile) -> bool:
    client = get_client()
    distance = DISTANCE_MAP.get(profile.qdrant_distance, qmodels.Distance.COSINE)

    try:
        client.get_collection(profile.qdrant_collection)
    except UnexpectedResponse:
        client.create_collection(
            profile.qdrant_collection,
            vectors_config=qmodels.VectorParams(
                size=profile.dimension,
                distance=distance,
            ),
            hnsw_config=qmodels.HnswConfigDiff(m=profile.hnsw_m, ef_construct=profile.hnsw_ef_construct),
        )
        return True
    return False


def upsert_vectors(profile: IndexProfile, records: Iterable[VectorRecord]) -> None:
    client = get_client()
    points = [
        qmodels.PointStruct(id=record.point_id, vector=record.vector, payload=record.payload)
        for record in records
    ]
    if points:
        client.upsert(profile.qdrant_collection, points=points, wait=True)


def delete_plan(profile: IndexProfile, plan: ChunkPlan) -> bool:
    client = get_client()
    selector: qtypes.PointsSelector = qmodels.FilterSelector(
        filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="plan_id",
                    match=qmodels.MatchValue(value=str(plan.plan_id)),
                )
            ]
        )
    )
    try:
        client.delete(collection_name=profile.qdrant_collection, points_selector=selector, wait=True)
    except UnexpectedResponse:
        return False
    return True


def healthcheck() -> dict[str, object]:
    """Return a structured health status for the configured Qdrant instance."""

    client = get_client()
    try:
        response = _invoke_health(client)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "error", "version": None, "commit": None, "detail": str(exc)}

    if isinstance(response, dict):
        status = response.get("status")
        version = response.get("version")
        commit = response.get("commit")
    else:
        status = getattr(response, "status", None)
        version = getattr(response, "version", None)
        commit = getattr(response, "commit", None)

    return {
        "ok": str(status).lower() == "ok" if status is not None else False,
        "status": status,
        "version": version,
        "commit": commit,
        "detail": "",
    }


def _invoke_health(client: QdrantClient):
    """Call the most appropriate health endpoint for the installed client version."""

    if hasattr(client, "health"):
        return client.health()

    openapi_client = getattr(client, "openapi_client", None)
    if openapi_client and hasattr(openapi_client, "health_api"):
        return openapi_client.health_api.health()

    http_client = getattr(client, "http", None)
    if http_client and hasattr(http_client, "health_api"):
        return http_client.health_api.health()

    # Fallback: perform a lightweight request that requires a healthy server.
    client.get_collections()
    return {"status": "ok", "version": None, "commit": None}


def list_collections() -> list[dict[str, Any]]:
    """Return a simplified list of available Qdrant collections."""

    client = get_client()
    response = client.get_collections()
    collections = []
    for collection in getattr(response, "collections", []):
        name = getattr(collection, "name", "")
        vectors_count = getattr(collection, "vectors_count", None)
        collections.append(
            {
                "name": name,
                "status": getattr(collection, "status", None),
                "vectors_count": int(vectors_count) if vectors_count is not None else None,
                "points_count": getattr(collection, "points_count", None),
                "segments_count": getattr(collection, "segments_count", None),
                "config": {},
            }
        )
    return collections


def describe_collection(collection_name: str) -> dict[str, Any] | None:
    """Return detailed information for a specific collection."""

    client = get_client()
    try:
        info = client.get_collection(collection_name)
    except UnexpectedResponse:
        return None

    payload = getattr(info, "dict", None)
    if callable(payload):
        data = payload()
    elif hasattr(info, "__dict__"):
        data = {k: v for k, v in info.__dict__.items() if not k.startswith("_")}
    else:
        data = {"status": getattr(info, "status", None)}

    return {
        "name": collection_name,
        "status": data.get("status"),
        "vectors_count": _coerce_int(data.get("vectors_count")),
        "points_count": _coerce_int(data.get("points_count")),
        "segments_count": _coerce_int(data.get("segments_count")),
        "config": _coerce_mapping(data.get("config") or data.get("payload_schema")),
    }


def summarize_plan_points(
    profile: IndexProfile,
    plan_id: UUID,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Return a snapshot of the stored vectors for a plan within a collection."""

    client = get_client()
    normalized_limit = max(1, min(limit, 256))
    plan_value = str(plan_id)

    filter_condition = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="plan_id",
                match=qmodels.MatchValue(value=plan_value),
            )
        ]
    )

    count_response = client.count(
        collection_name=profile.qdrant_collection,
        filter=filter_condition,
        exact=True,
    )
    total_points = int(getattr(count_response, "count", 0))

    scroll_points, next_offset = client.scroll(
        collection_name=profile.qdrant_collection,
        scroll_filter=filter_condition,
        limit=normalized_limit,
        with_payload=True,
        with_vectors=False,
    )

    points_payload: list[dict[str, Any]] = []
    for record in scroll_points:
        point_id = getattr(record, "id", None)
        payload = getattr(record, "payload", {}) or {}
        points_payload.append(
            {
                "id": str(point_id),
                "payload": payload,
            }
        )

    return {
        "collection": profile.qdrant_collection,
        "plan_id": plan_value,
        "limit": normalized_limit,
        "returned": len(points_payload),
        "total": total_points,
        "next_offset": str(next_offset) if next_offset is not None else None,
        "points": points_payload,
    }


def search_collection(
    profile: IndexProfile,
    vector: list[float],
    *,
    limit: int,
    score_threshold: float | None = None,
    with_payload: bool = True,
    with_vectors: bool = False,
):
    """Execute a vector search against a profile's collection."""

    client = get_client()
    search_kwargs: dict[str, Any] = {
        "collection_name": profile.qdrant_collection,
        "query_vector": vector,
        "limit": limit,
        "with_payload": with_payload,
        "with_vectors": with_vectors,
    }
    if score_threshold is not None:
        search_kwargs["score_threshold"] = score_threshold

    return client.search(**search_kwargs)


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "dict"):
        return value.dict()
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    return {}
