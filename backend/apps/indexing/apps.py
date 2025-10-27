from django.apps import AppConfig


class IndexingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "backend.apps.indexing"
    verbose_name = "Indexing"

    def ready(self) -> None:  # pragma: no cover - import side effects only
        from . import tasks  # noqa: F401
        from . import signals  # noqa: F401
        from . import checks  # noqa: F401
