from django.urls import include, path
from rest_framework.routers import DefaultRouter

from backend.apps.pastpaper.api.views import PastPaperViewSet

app_name = "pastpaper_api"

router = DefaultRouter()
router.register(r"", PastPaperViewSet, basename="pastpaper")

urlpatterns = [
    path("", include(router.urls)),
]
