from django.db import models


class TestFile(models.Model):
    """Simple model to persist uploaded files for integration testing."""

    file = models.FileField(upload_to="test_files/")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:  # pragma: no cover - human readable display only
        return f"TestFile(id={self.pk}, name={self.file.name})"

    @property
    def filename(self) -> str:
        """Return the basename of the stored file for convenience."""

        return self.file.name.rsplit("/", 1)[-1]


from .ioqueue.models import *
