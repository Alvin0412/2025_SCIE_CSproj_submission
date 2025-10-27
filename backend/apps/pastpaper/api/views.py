import logging
from typing import Any, Dict, List
from django.http import HttpResponseRedirect
from drf_yasg.inspectors.field import limit_validators
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import MethodNotAllowed
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from django.contrib.postgres.search import TrigramSimilarity
from django.db.models.functions import Length, Lower

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, PolymorphicProxySerializer, extend_schema

from backend.apps.pastpaper.models import PastPaper, PastPaperComponent
from backend.apps.pastpaper.tasks import trigger_parse_async, trigger_test_task
from backend.apps.pastpaper.api.serializers import (
    PastPaperSerializer,
    PastPaperCreateSerializer,
    PastPaperAppendSerializer,
    PastPaperStateUpdateSerializer,
    PastPaperListQuerySerializer,
    PastPaperRetrieveQuerySerializer,
    PastPaperPDFResponseSerializer,
    PastPaperParsedResponseSerializer,
    PastPaperListByCodeResponseSerializer,
    PastPaperStateUpdateResponseSerializer,
    PastPaperDeleteRequestSerializer,
    PastPaperDeleteResponseSerializer,
    PastPaperComponentsQuerySerializer,
    PastPaperComponentsResponseSerializer,
    PastPaperParseRequestSerializer,
    PastPaperParseResponseSerializer,
    PastPaperReparseErrorsRequestSerializer,
    PastPaperReparseErrorsResponseSerializer,
    PastPaperTestTaskRequestSerializer,
    PastPaperTestTaskResponseSerializer,
    PastPaperComponentSearchQuerySerializer,
    PastPaperComponentSearchResponseSerializer,
)

logger = logging.getLogger(__name__)

PAST_PAPER_LIST_RESPONSE = PolymorphicProxySerializer(
    component_name="PastPaperListResponse",
    resource_type_field_name=None,
    serializers=[
        PastPaperSerializer,
        PastPaperPDFResponseSerializer,
        PastPaperParsedResponseSerializer,
        PastPaperListByCodeResponseSerializer,
    ],
)

PAST_PAPER_RETRIEVE_RESPONSE = PolymorphicProxySerializer(
    component_name="PastPaperRetrieveResponse",
    resource_type_field_name=None,
    serializers=[
        PastPaperSerializer,
        PastPaperPDFResponseSerializer,
        PastPaperParsedResponseSerializer,
    ],
)


class PastPaperViewSet(viewsets.ViewSet):
    """
    PastPaper 只通过版本追加修改（append-only）。
    查询支持 paper_id / paper_code，type=asset|pdf|parsed。
    """

    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    # Resolve which stored version should back a request for a paper_id.
    def _pick_paper_by_id(self, paper_id: str, *, active_only: bool, version_no: int | None = None):
        qs = PastPaper.objects.filter(paper_id=paper_id).order_by("-version_no")
        if version_no is not None:
            return qs.filter(version_no=version_no).first()
        if active_only:
            paper = qs.filter(is_active=True).first()
            if paper:
                return paper
        return qs.first()

    # Return the appropriate representation (asset/pdf/parsed) for a single paper.
    def _serialize_single_paper(self, paper: PastPaper, typ: str, redirect: bool):
        if typ == "pdf":
            pdf_url = paper.asset.file.url if paper.asset and paper.asset.file else ""
            if redirect and pdf_url:
                return HttpResponseRedirect(pdf_url)
            return Response({"paper_id": str(paper.paper_id), "version_no": paper.version_no, "pdf_url": pdf_url})
        if typ == "parsed":
            return Response(
                {
                    "paper_id": str(paper.paper_id),
                    "version_no": paper.version_no,
                    "parsed_state": paper.parsed_state,
                    "parsed_tree": paper.parsed_tree,
                }
            )
        return Response(PastPaperSerializer(paper).data)

    @extend_schema(
        request=PastPaperCreateSerializer,
        responses={
            201: PastPaperSerializer,
            400: OpenApiResponse(description="Invalid payload"),
        },
    )
    def create(self, request, *args, **kwargs):
        """Create a brand-new past paper record from uploaded metadata and file."""
        ser = PastPaperCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        logger.info("PastPaper create requested", extra={"paper_code": ser.validated_data.get("paper_code")})
        paper = ser.save()
        logger.info(
            "PastPaper created",
            extra={"paper_id": str(paper.paper_id), "version_no": paper.version_no},
        )
        return Response(PastPaperSerializer(paper).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        parameters=[PastPaperListQuerySerializer],
        responses={
            200: PAST_PAPER_LIST_RESPONSE,
            302: OpenApiResponse(description="Redirect to PDF asset when redirect=true"),
            400: OpenApiResponse(description="Missing or invalid query parameters"),
            404: OpenApiResponse(description="Past paper not found"),
        },
    )
    def list(self, request, *args, **kwargs):
        """Handle list endpoint supporting lookups by paper_id, paper_code, or redirects."""
        paper_id = request.query_params.get("paper_id")
        paper_code = request.query_params.get("paper_code")
        typ = (request.query_params.get("type") or "asset").lower()
        active_only = request.query_params.get("active_only") in ("true", "1")
        redirect = request.query_params.get("redirect") in ("true", "1")
        version_no_raw = request.query_params.get("version_no")
        if version_no_raw is not None:
            try:
                version_no = int(version_no_raw)
            except ValueError:
                return Response({"detail": "version_no must be an integer"}, status=400)
        else:
            version_no = None

        if paper_id:
            paper = self._pick_paper_by_id(
                paper_id,
                active_only=active_only,
                version_no=version_no,
            )
            if not paper:
                logger.warning(
                    "PastPaper list lookup failed",
                    extra={"paper_id": paper_id, "version_no": version_no},
                )
                return Response({"detail": "not found"}, status=404)
            result = self._serialize_single_paper(paper, typ, redirect)
            if isinstance(result, HttpResponseRedirect):
                return result
            return result

        if paper_code:
            qs = PastPaper.objects.filter(metadata__paper_code=paper_code).order_by("-created_at")
            if active_only:
                qs = qs.filter(is_active=True)
            logger.info(
                "PastPaper list by code",
                extra={"paper_code": paper_code, "active_only": active_only, "count": qs.count()},
            )
            payload = PastPaperListByCodeResponseSerializer(
                {
                    "paper_code": paper_code,
                    "count": qs.count(),
                    "results": qs,
                }
            ).data
            return Response(payload)

        return Response({"detail": "need paper_id or paper_code"}, status=400)

    @extend_schema(
        parameters=[PastPaperRetrieveQuerySerializer],
        responses={
            200: PAST_PAPER_RETRIEVE_RESPONSE,
            302: OpenApiResponse(description="Redirect to PDF asset when redirect=true"),
            400: OpenApiResponse(description="Missing paper_id or invalid query parameters"),
            404: OpenApiResponse(description="Past paper not found"),
        },
    )
    def retrieve(self, request, pk=None, *args, **kwargs):
        """Retrieve a single past paper version via router-provided pk."""
        if pk is None:
            logger.warning("Retrieve called without paper_id")
            return Response({"detail": "missing paper_id"}, status=400)
        typ = (request.query_params.get("type") or "asset").lower()
        redirect = request.query_params.get("redirect") in ("true", "1")
        active_only = request.query_params.get("active_only") in ("true", "1")
        version_no_raw = request.query_params.get("version_no")
        if version_no_raw is not None:
            try:
                version_no = int(version_no_raw)
            except ValueError:
                return Response({"detail": "version_no must be an integer"}, status=400)
        else:
            version_no = None

        paper = self._pick_paper_by_id(
            pk,
            active_only=active_only,
            version_no=version_no,
        )
        if not paper:
            logger.warning(
                "PastPaper retrieve lookup failed",
                extra={"paper_id": pk, "version_no": version_no},
            )
            return Response({"detail": "not found"}, status=404)

        result = self._serialize_single_paper(paper, typ, redirect)
        if isinstance(result, HttpResponseRedirect):
            return result
        return result

    def update(self, request, *args, **kwargs):
        """Disallow full updates; versions are append-only."""
        raise MethodNotAllowed("PUT")

    def partial_update(self, request, *args, **kwargs):
        """Disallow partial updates; versions are append-only."""
        raise MethodNotAllowed("PATCH")

    def destroy(self, request, *args, **kwargs):
        """Disallow instance deletes via the default destroy method."""
        raise MethodNotAllowed("DELETE")

    @extend_schema(
        request=PastPaperAppendSerializer,
        responses={
            201: PastPaperSerializer,
            400: OpenApiResponse(description="Invalid payload"),
        },
    )
    @action(detail=False, methods=["put"], url_path="append")
    def append(self, request, *args, **kwargs):
        """Append a new version to an existing logical past paper."""
        ser = PastPaperAppendSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        logger.info(
            "PastPaper append requested",
            extra={"paper_id": str(ser.validated_data.get("paper_id"))},
        )
        paper = ser.save()
        logger.info(
            "PastPaper version appended",
            extra={"paper_id": str(paper.paper_id), "version_no": paper.version_no},
        )
        return Response(PastPaperSerializer(paper).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=PastPaperStateUpdateSerializer,
        responses={
            200: PastPaperStateUpdateResponseSerializer,
            400: OpenApiResponse(description="Invalid payload or missing past paper"),
        },
    )
    @action(detail=False, methods=["patch"], url_path="state")
    def update_state(self, request, *args, **kwargs):
        """Modify processing state fields for a specific paper version."""
        ser = PastPaperStateUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        logger.info(
            "PastPaper state update requested",
            extra={
                "paper_id": str(ser.validated_data.get("paper_id")),
                "version_no": ser.validated_data.get("version_no"),
                "parsed_state": ser.validated_data.get("parsed_state"),
            },
        )
        paper = ser.update(None, ser.validated_data)
        logger.info(
            "PastPaper state updated",
            extra={"paper_id": str(paper.paper_id), "version_no": paper.version_no},
        )
        return Response(PastPaperStateUpdateResponseSerializer(paper).data, status=200)

    @extend_schema(
        request=PastPaperParseRequestSerializer,
        responses={
            202: PastPaperParseResponseSerializer,
            400: OpenApiResponse(description="Invalid payload"),
            404: OpenApiResponse(description="Past paper not found"),
        },
    )
    @action(detail=False, methods=["post"], url_path="parse")
    def trigger_parse(self, request, *args, **kwargs):
        """Manually enqueue the parsing task for a past paper."""
        ser = PastPaperParseRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        paper_id = ser.validated_data["paper_id"]
        version_no = ser.validated_data.get("version_no")
        use_image = ser.validated_data.get("use_image")

        qs = PastPaper.objects.filter(paper_id=paper_id).order_by("-version_no")
        paper = qs.first() if version_no is None else qs.filter(version_no=version_no).first()
        if not paper:
            logger.warning(
                "Manual parse requested for missing paper",
                extra={"paper_id": str(paper_id), "version_no": version_no},
            )
            return Response({"detail": "not found"}, status=status.HTTP_404_NOT_FOUND)

        trigger_parse_async(
            paper_id=str(paper.paper_id),
            version_no=paper.version_no,
            use_image=use_image,
        )
        logger.info(
            "Manual parse enqueued",
            extra={
                "paper_id": str(paper.paper_id),
                "version_no": paper.version_no,
                "use_image": use_image,
            },
        )

        payload = PastPaperParseResponseSerializer(
            {
                "enqueued": True,
                "paper_id": paper.paper_id,
                "version_no": paper.version_no,
                "parsed_state": paper.parsed_state,
            }
        ).data
        return Response(payload, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        request=PastPaperReparseErrorsRequestSerializer,
        responses={
            200: PastPaperReparseErrorsResponseSerializer,
            202: PastPaperReparseErrorsResponseSerializer,
        },
    )
    @action(detail=False, methods=["post"], url_path="reparse-errors")
    def reparse_errors(self, request, *args, **kwargs):
        """Reset and re-enqueue parsing for every paper currently in ERROR state."""
        ser = PastPaperReparseErrorsRequestSerializer(data=request.data or {})
        ser.is_valid(raise_exception=True)

        use_image = ser.validated_data.get("use_image")
        limit = ser.validated_data.get("limit")

        with transaction.atomic():
            papers = list(
                PastPaper.objects.select_for_update().filter(parsed_state="ERROR").order_by("updated_at")[:limit]
            )

            if not papers:
                logger.info("Reparse requested but no papers in ERROR state")
                payload = PastPaperReparseErrorsResponseSerializer(
                    {"enqueued": False, "count": 0, "papers": []}
                ).data
                return Response(payload, status=status.HTTP_200_OK)

            ids = [paper.id for paper in papers]
            now = timezone.now()
            PastPaper.objects.filter(id__in=ids).update(
                parsed_state="PENDING",
                last_error="",
                parsed_tree=None,
                updated_at=now,
            )

            for paper in papers:
                trigger_parse_async(
                    paper_id=str(paper.paper_id),
                    version_no=paper.version_no,
                    use_image=use_image,
                )

        logger.info(
            "Reparse enqueued for error past papers",
            extra={
                "count": len(papers),
                "paper_ids": [str(p.paper_id) for p in papers],
            },
        )

        payload = PastPaperReparseErrorsResponseSerializer(
            {
                "enqueued": True,
                "count": len(papers),
                "papers": [
                    {"paper_id": paper.paper_id, "version_no": paper.version_no}
                    for paper in papers
                ],
            }
        ).data
        return Response(payload, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        request=PastPaperTestTaskRequestSerializer,
        responses={202: PastPaperTestTaskResponseSerializer},
    )
    @action(detail=False, methods=["post"], url_path="test-task")
    def test_task(self, request, *args, **kwargs):
        """Enqueue a lightweight Dramatiq task to verify worker availability."""
        ser = PastPaperTestTaskRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        payload = ser.validated_data.get("payload", "")
        message_id = trigger_test_task(payload)
        logger.info(
            "Test Dramatiq task enqueued",
            extra={"message_id": message_id, "payload": payload},
        )

        response_payload = PastPaperTestTaskResponseSerializer(
            {
                "enqueued": True,
                "message_id": message_id,
                "payload": payload,
            }
        ).data
        return Response(response_payload, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        parameters=[PastPaperDeleteRequestSerializer],
        request=PastPaperDeleteRequestSerializer,
        responses={
            200: PastPaperDeleteResponseSerializer,
            400: OpenApiResponse(description="Missing paper_id"),
            404: OpenApiResponse(description="Past paper version not found"),
        },
    )
    @action(detail=False, methods=["delete"], url_path="delete")
    def delete_version(self, request, *args, **kwargs):
        paper_id = request.data.get("paper_id") or request.query_params.get("paper_id")
        version_no = request.data.get("version_no") or request.query_params.get("version_no")
        if not paper_id:
            return Response({"detail": "need paper_id"}, status=400)

        with transaction.atomic():
            qs = PastPaper.objects.select_for_update().filter(paper_id=paper_id).order_by("-version_no")
            paper = qs.filter(version_no=version_no).first() if version_no else qs.first()
            if not paper:
                return Response({"deleted": False, "detail": "not found"}, status=404)

            asset = paper.asset
            metadata = paper.metadata

            paper.delete()

            remaining = PastPaper.objects.filter(paper_id=paper_id).order_by("-version_no")
            if remaining.exists():
                remaining.update(is_active=False)
                latest = remaining.first()
                latest.is_active = True
                latest.save(update_fields=["is_active"])

                if asset and not PastPaper.objects.filter(asset=asset).exists():
                    asset.delete()

            else:
                if asset:
                    asset.delete()
                if metadata:
                    metadata.delete()

        return Response({"deleted": True, "paper_id": paper_id, "version_no": version_no or "latest"})

    @extend_schema(
        parameters=[PastPaperComponentsQuerySerializer],
        responses={
            200: PastPaperComponentsResponseSerializer,
            400: OpenApiResponse(description="Missing paper_id"),
            404: OpenApiResponse(description="Past paper not found"),
        },
    )
    @action(detail=False, methods=["get"], url_path="components")
    def components(self, request, *args, **kwargs):
        """Expose the hierarchical or flat component tree for a paper."""
        paper_id = request.query_params.get("paper_id")
        if not paper_id:
            return Response({"detail": "need paper_id"}, status=400)
        version_no = request.query_params.get("version_no")
        flat = request.query_params.get("flat") in ("true", "1")
        path_prefix = request.query_params.get("path_prefix") or ""

        qs = PastPaper.objects.filter(paper_id=paper_id).order_by("-version_no")
        paper = qs.first() if not version_no else qs.filter(version_no=version_no).first()
        if not paper:
            logger.warning(
                "Components lookup failed",
                extra={"paper_id": paper_id, "version_no": version_no},
            )
            return Response({"detail": "not found"}, status=404)

        comps_qs = paper.components.all().order_by("depth", "id")
        if path_prefix:
            comps_qs = comps_qs.filter(path_normalized__startswith=path_prefix)
        logger.info(
            "Components queried",
            extra={
                "paper_id": paper_id,
                "version_no": paper.version_no,
                "flat": flat,
                "path_prefix": path_prefix,
            },
        )

        if flat:
            data = [
                {
                    "id": c.id,
                    "num_display": c.num_display,
                    "path_normalized": c.path_normalized,
                    "depth": c.depth,
                    "page": c.page,
                    "score": float(c.score) if c.score is not None else None,
                    "content": c.content,
                    "position": c.position,
                }
                for c in comps_qs
            ]
            payload = PastPaperComponentsResponseSerializer(
                {
                    "paper_id": paper_id,
                    "version_no": paper.version_no,
                    "count": len(data),
                    "results": data,
                }
            ).data
            return Response(payload)

        by_parent: Dict[Any, List[Any]] = {}
        nodes: Dict[int, Dict[str, Any]] = {}
        for c in comps_qs:
            node = {
                "id": c.id,
                "num_display": c.num_display,
                "path_normalized": c.path_normalized,
                "depth": c.depth,
                "page": c.page,
                "score": float(c.score) if c.score is not None else None,
                "content": c.content,
                "position": c.position,
                "children": [],
            }
            nodes[c.id] = node
            pid = c.parent_id or 0
            by_parent.setdefault(pid, []).append(node)

        for c in comps_qs:
            if c.parent_id:
                parent = nodes.get(c.parent_id)
                if parent:
                    parent["children"].append(nodes[c.id])

        tree = by_parent.get(0, [])
        payload = PastPaperComponentsResponseSerializer(
            {
                "paper_id": paper_id,
                "version_no": paper.version_no,
                "components": tree,
            }
        ).data
        return Response(payload)


    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="keyword",
                location=OpenApiParameter.QUERY,
                required=True,
                type=str,
                description="Keyword to search within component content, number, or path.",
            ),
            OpenApiParameter(
                name="limit",
                location=OpenApiParameter.QUERY,
                required=False,
                type=int,
                description="Maximum number of matches to return (default 20, max 100).",
            ),
            OpenApiParameter(
                name="offset",
                location=OpenApiParameter.QUERY,
                required=False,
                type=int,
                description="Number of matches to skip before returning results.",
            ),
            OpenApiParameter(
                name="paper_id",
                location=OpenApiParameter.QUERY,
                required=False,
                type=str,
                description="Optional filter to restrict matches to a specific paper UUID.",
            ),
            OpenApiParameter(
                name="fuzzy",
                location=OpenApiParameter.QUERY,
                required=False,
                type=bool,
                description="Set to false to disable trigram similarity and use basic icontains filtering.",
            ),
        ],
        responses={
            200: PastPaperComponentSearchResponseSerializer,
            400: OpenApiResponse(description="Missing required keyword parameter."),
        },
    )
    @action(detail=False, methods=["get"], url_path="component-search")
    def component_search(self, request, *args, **kwargs):
        """Keyword match across components with simple offset pagination."""

        query_serializer = PastPaperComponentSearchQuerySerializer(data=request.query_params)
        query_serializer.is_valid(raise_exception=True)
        params = query_serializer.validated_data

        keyword: str = params["keyword"].strip()
        if not keyword:
            return Response({"detail": "keyword must not be blank"}, status=400)
        limit = params.get("limit", 20)
        offset = params.get("offset", 0)
        paper_uuid = params.get("paper_id")
        use_fuzzy = params.get("fuzzy", True)

        keyword_length = len(keyword)

        qs = PastPaperComponent.objects.select_related("paper")

        if use_fuzzy:
            # threshold = 0.2 if keyword_length >= 3 else 0.1
            qs = qs.annotate(
                similarity=TrigramSimilarity(Lower("content"), keyword.lower()) / (1 + Length("content") / 500.0)
            )

            if keyword_length < 3:
                qs = qs.filter(
                    Q(content__icontains=keyword)
                    | Q(num_display__icontains=keyword)
                    | Q(path_normalized__icontains=keyword)
                )

            qs = qs.order_by("-similarity", "-updated_at", "id")
        else:
            qs = qs.filter(
                Q(content__icontains=keyword)
                | Q(num_display__icontains=keyword)
                | Q(path_normalized__icontains=keyword)
            ).order_by("-updated_at", "id")

        if paper_uuid:
            qs = qs.filter(paper__paper_id=paper_uuid)

        total = qs.count()
        components = list(qs[offset : offset + limit])

        results: List[Dict[str, Any]] = []
        for component in components:
            paper = component.paper
            results.append(
                {
                    "component_id": component.id,
                    "paper_pk": component.paper_id,
                    "paper_id": str(paper.paper_id),
                    "version_no": paper.version_no,
                    "num_display": component.num_display or "",
                    "path_normalized": component.path_normalized or "",
                    "depth": component.depth,
                    "page": component.page,
                    "content": component.content or "",
                    "similarity": float(
                        getattr(component, "similarity", 1.0 if not use_fuzzy else 0.0)
                    ),
                }
            )

        base_url = request.build_absolute_uri(request.path)

        def build_url(new_offset: int | None) -> str | None:
            if new_offset is None:
                return None
            query = request.query_params.copy()
            if not query.get("keyword"):
                query["keyword"] = keyword
            query["limit"] = str(limit)
            query["offset"] = str(max(new_offset, 0))
            if paper_uuid:
                query["paper_id"] = str(paper_uuid)
            query["fuzzy"] = "true" if use_fuzzy else "false"
            query_string = query.urlencode()
            return f"{base_url}?{query_string}" if query_string else base_url

        next_offset = offset + limit if (offset + limit) < total else None
        prev_offset_value = offset - limit if offset > 0 else None
        if prev_offset_value is not None and prev_offset_value < 0:
            prev_offset_value = 0

        response_payload = {
            "count": total,
            "next": build_url(next_offset),
            "previous": build_url(prev_offset_value),
            "results": results,
        }

        response_serializer = PastPaperComponentSearchResponseSerializer(response_payload)
        return Response(response_serializer.data)
