"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

# backend/config/asgi.py
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.config.settings")
django.setup()  # <<< 必须在 import 业务模块之前

import asyncio

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler

from backend.apps.retrieval.routing import websocket_urlpatterns


django_asgi_app = get_asgi_application()
application = ProtocolTypeRouter({
    "http": ASGIStaticFilesHandler(django_asgi_app),
    "websocket": AuthMiddlewareStack(
        URLRouter(websocket_urlpatterns)
    ),
})

# 引入初始化逻辑
from backend.apps.service.orchestrators.service import ResultOrchestrator  # noqa
from backend.apps.service.orchestrators.registry import start_cleanup  # noqa


async def startup():
    global _orch
    _orch = ResultOrchestrator()
    await _orch.start()
    print("ResultOrchestrator started successfully")

    start_cleanup()
    print("FutureRegistry cleanup service started")


try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        asyncio.ensure_future(startup())
    else:
        loop.run_until_complete(startup())
except RuntimeError:
    asyncio.run(startup())
