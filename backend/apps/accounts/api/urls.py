from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import AccountStatsViewSet, AuthViewSet, MembershipViewSet, RoleViewSet

router = DefaultRouter()
router.register("auth", AuthViewSet, basename="auth")
router.register("roles", RoleViewSet, basename="role")
router.register("memberships", MembershipViewSet, basename="membership")
router.register("stats", AccountStatsViewSet, basename="account-stats")

urlpatterns = [
    path("", include(router.urls)),
]
