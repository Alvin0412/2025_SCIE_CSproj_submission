import hashlib
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from backend.apps.pastpaper.models import PastPaper, PastPaperAsset, PastPaperMetadata


class PastPaperReparseErrorsTests(APITestCase):
    def setUp(self) -> None:
        self.url = reverse("pastpaper_api:pastpaper-reparse-errors")

    def _create_paper(self, *, suffix: str, parsed_state: str) -> PastPaper:
        metadata = PastPaperMetadata.objects.create(
            paper_code=f"code-{suffix}",
            exam_board="CAIE",
            subject="Mathematics",
            syllabus_code="0606",
            season="May/Jun",
            year=2024,
            variant_no="11",
            paper_type="qp",
        )

        content = f"%PDF-1.4 sample {suffix}".encode()
        checksum = hashlib.sha256(content).hexdigest()
        uploaded = SimpleUploadedFile(
            name=f"{suffix}.pdf",
            content=content,
            content_type="application/pdf",
        )
        asset = PastPaperAsset.objects.create(
            file=uploaded,
            mime="application/pdf",
            size=len(content),
            checksum_sha256=checksum,
            pages=None,
            url_source="",
        )

        return PastPaper.objects.create(
            metadata=metadata,
            asset=asset,
            parsed_state=parsed_state,
            last_error="boom" if parsed_state == "ERROR" else "",
            parsed_tree={"foo": "bar"} if parsed_state == "ERROR" else None,
        )

    def test_enqueues_and_resets_every_error_paper(self) -> None:
        error_one = self._create_paper(suffix="error-one", parsed_state="ERROR")
        error_two = self._create_paper(suffix="error-two", parsed_state="ERROR")
        ready = self._create_paper(suffix="ready", parsed_state="READY")

        with patch("backend.apps.pastpaper.api.views.trigger_parse_async") as mock_trigger:
            response = self.client.post(self.url, data={"use_image": True}, format="json")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["count"], 2)
        self.assertTrue(response.data["enqueued"])

        mock_trigger.assert_called()
        self.assertEqual(mock_trigger.call_count, 2)
        expected_calls = {
            (str(error_one.paper_id), error_one.version_no),
            (str(error_two.paper_id), error_two.version_no),
        }
        actual_calls = {
            (call.kwargs.get("paper_id") or call.args[0], call.kwargs.get("version_no") or call.args[1])
            for call in mock_trigger.call_args_list
        }
        self.assertSetEqual(expected_calls, actual_calls)
        for call in mock_trigger.call_args_list:
            self.assertTrue(call.kwargs.get("use_image"))

        for paper in (error_one, error_two, ready):
            paper.refresh_from_db()

        self.assertEqual(error_one.parsed_state, "PENDING")
        self.assertEqual(error_one.last_error, "")
        self.assertIsNone(error_one.parsed_tree)

        self.assertEqual(error_two.parsed_state, "PENDING")
        self.assertEqual(error_two.last_error, "")
        self.assertIsNone(error_two.parsed_tree)

        self.assertEqual(ready.parsed_state, "READY")

    def test_no_error_papers_returns_noop(self) -> None:
        ready = self._create_paper(suffix="ready", parsed_state="READY")

        with patch("backend.apps.pastpaper.api.views.trigger_parse_async") as mock_trigger:
            response = self.client.post(self.url, data={}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["enqueued"])
        self.assertEqual(response.data["count"], 0)
        mock_trigger.assert_not_called()

        ready.refresh_from_db()
        self.assertEqual(ready.parsed_state, "READY")
