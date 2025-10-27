from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart

from .models import TestFile


class TestFileApiTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.media_root = Path(tempfile.mkdtemp(prefix="service-media-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.media_root, ignore_errors=True))

    def test_file_crud_flow(self) -> None:
        upload = SimpleUploadedFile("hello.txt", b"hello world", content_type="text/plain")

        with override_settings(MEDIA_ROOT=self.media_root):
            # Create
            create_response = self.client.post("/api/service/files/", {"file": upload})
            self.assertEqual(create_response.status_code, 201)
            created_payload = create_response.json()

            test_file_id = created_payload["id"]
            self.assertEqual(created_payload["filename"], "hello.txt")
            stored_path = self.media_root / "test_files" / "hello.txt"
            self.assertTrue(stored_path.exists())
            self.assertEqual(stored_path.read_bytes(), b"hello world")

            # List
            list_response = self.client.get("/api/service/files/")
            self.assertEqual(list_response.status_code, 200)
            files = list_response.json()
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["id"], test_file_id)

            # Retrieve
            retrieve_response = self.client.get(f"/api/service/files/{test_file_id}")
            self.assertEqual(retrieve_response.status_code, 200)
            self.assertEqual(retrieve_response.json()["filename"], "hello.txt")

            # Update (replace underlying file)
            existing_path = Path(TestFile.objects.get(pk=test_file_id).file.path)
            replacement = SimpleUploadedFile(
                "updated.txt", b"content v2", content_type="text/plain"
            )
            payload = encode_multipart(BOUNDARY, {"file": replacement})
            update_response = self.client.put(
                f"/api/service/files/{test_file_id}",
                data=payload,
                content_type=MULTIPART_CONTENT,
            )
            self.assertEqual(update_response.status_code, 200)
            updated_payload = update_response.json()
            self.assertEqual(updated_payload["filename"], "updated.txt")

            updated_instance = TestFile.objects.get(pk=test_file_id)
            new_path = Path(updated_instance.file.path)
            self.assertFalse(existing_path.exists())
            self.assertTrue(new_path.exists())
            self.assertEqual(new_path.read_bytes(), b"content v2")

            # Delete
            delete_response = self.client.delete(f"/api/service/files/{test_file_id}")
            self.assertEqual(delete_response.status_code, 204)
            self.assertFalse(TestFile.objects.filter(pk=test_file_id).exists())
            self.assertFalse(new_path.exists())
