# backend/apps/retrieval/views.py
from typing import Any, Dict

from rest_framework.permissions import AllowAny
from rest_framework.renderers import TemplateHTMLRenderer
from rest_framework.response import Response
from rest_framework.views import APIView


class DemoPageView(APIView):
    permission_classes = [AllowAny]
    renderer_classes = [TemplateHTMLRenderer]
    template_name = "realtime_demo.html"

    def get(self, request, *args, **kwargs):
        ctx: Dict[str, Any] = {"ws_url": "/ws/retrieval/"}
        return Response(ctx)
