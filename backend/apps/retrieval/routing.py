from django.urls import path, re_path

from backend.apps.retrieval.consumer import RetrievalConsumer
from backend.apps.retrieval.views import DemoPageView

websocket_urlpatterns = [
    re_path(r"^ws/retrieval/$", RetrievalConsumer.as_asgi()),
]

urlpatterns = [
    re_path(r"^$", DemoPageView.as_view(), name="realtime_demo"),
]
