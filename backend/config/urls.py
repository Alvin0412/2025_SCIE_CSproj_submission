"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as.view(), name='home')
Including another URLconf
    1. Import the include function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from backend.apps.service.api import api as service_api
from backend.apps.service.orchestrators.views import resolve_task

from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)

# from drf_yasg.views import get_schema_view
# from drf_yasg import openapi

# from backend.apps.pastpaper.api_v2 import api as pastpaper_api_v2

# schema_view = get_schema_view(
#     openapi.Info(title="My API", default_version="v1"),
#     public=True,
#     permission_classes=[permissions.AllowAny],
# )

urlpatterns = [
    path("api/admin/", admin.site.urls),
    path("api/_orchestrator/resolve", resolve_task, name="orchestrator_resolve"),
    # path("api/orchestrator/stats/", registry_stats, name="registry_stats"),
    # path("api/orchestrator/pending/", pending_futures, name="pending_futures"),
    # path("api/orchestrator/cleanup/", cleanup_registry, name="cleanup_registry"),
    # path("api/orchestrator/cleanup/start/", start_cleanup_service, name="start_cleanup"),
    # path("api/orchestrator/cleanup/stop/", stop_cleanup_service, name="stop_cleanup"),
    path("api/service/", service_api.urls),
    path("api/accounts/", include("backend.apps.accounts.api.urls"), name="accounts_api"),
    path("api/pastpaper/v1/", include("backend.apps.pastpaper.api.urls"), name="pastpaper_api"),
    path("api/indexing/", include("backend.apps.indexing.api.urls"), name="indexing_api"),
    # path("api/pastpaper/v2/", pastpaper_api_v2.urls),
    # TODO: I hate django-ninja
    path("api/realtime-demo/", include("backend.apps.retrieval.routing"), name="realtime_demo"),
]

# urlpatterns += [
#     re_path(r"^swagger(?P<format>\.json|\.yaml)$", schema_view.without_ui(cache_timeout=0), name="schema-json"),
#     path("swagger/", schema_view.with_ui("swagger", cache_timeout=0), name="schema-swagger-ui"),
#     path("redoc/", schema_view.with_ui("redoc", cache_timeout=0), name="schema-redoc"),
# ]

urlpatterns += [
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/schema/swagger-ui/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/schema/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]

# TODO: Add nginx support for production
# TODO: use OSS when deployed
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
