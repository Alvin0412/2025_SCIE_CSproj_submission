"""URL configuration for the indexing DRF endpoints."""

from rest_framework.routers import DefaultRouter

from .views import (
    BundleViewSet,
    ChunkPlanViewSet,
    ChunkViewSet,
    IndexProfileViewSet,
    IndexingTestingViewSet,
    QdrantSearchViewSet,
)

router = DefaultRouter()
router.register(r"profiles", IndexProfileViewSet, basename="indexprofile")
router.register(r"plans", ChunkPlanViewSet, basename="chunkplan")
router.register(r"bundles", BundleViewSet, basename="bundle")
router.register(r"chunks", ChunkViewSet, basename="chunk")
router.register(r"testing", IndexingTestingViewSet, basename="indexing-testing")
router.register(r"search", QdrantSearchViewSet, basename="indexing-search")

urlpatterns = router.urls
