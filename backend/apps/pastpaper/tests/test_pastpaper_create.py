from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase


class PastPaperCreateTests(APITestCase):
    def setUp(self) -> None:
        self.url = reverse("pastpaper_api:pastpaper-list")

    def _payload(self, *, paper_code: str) -> dict:
        file_content = b"%PDF-1.4 sample"
        sample_file = SimpleUploadedFile(
            "sample.pdf",
            file_content,
            content_type="application/pdf",
        )
        return {
            "paper_code": paper_code,
            "exam_board": "CAIE",
            "subject": "Mathematics",
            "year": 2024,
            "syllabus_code": "0606",
            "season": "May/Jun",
            "variant_no": "42",
            "paper_type": "qp",
            "file": sample_file,
        }

    def test_duplicate_paper_code_rejected(self):
        first_response = self.client.post(self.url, data=self._payload(paper_code="9489_w22_ms_32"), format="multipart")
        self.assertEqual(first_response.status_code, status.HTTP_201_CREATED)

        duplicate_response = self.client.post(
            self.url,
            data=self._payload(paper_code="9489_w22_ms_32"),
            format="multipart",
        )
        self.assertEqual(duplicate_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("paper_code", duplicate_response.data)
