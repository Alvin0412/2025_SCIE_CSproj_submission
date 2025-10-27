from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "backend.apps.accounts"

    def ready(self):
        # Import signals that maintain metadata and memberships.
        from . import signals  # noqa: F401
