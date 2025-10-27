from __future__ import annotations

from django.test import TestCase

from backend.apps.pastpaper.models import (
    PastPaper,
    PastPaperAsset,
    PastPaperComponent,
    PastPaperMetadata,
)
from backend.apps.pastpaper.tool import KeywordQuery, search_components


class KeywordSearchToolTests(TestCase):
    def setUp(self) -> None:
        self.asset = PastPaperAsset.objects.create(
            file="pastpapers/test.pdf",
            mime="application/pdf",
            size=1,
            checksum_sha256="a" * 64,
        )
        self.metadata_econ = PastPaperMetadata.objects.create(
            paper_code="9708_w20_qp_12",
            exam_board="CAIE",
            subject="Economics",
            syllabus_code="9708",
            season="Winter",
            year=2020,
            paper_type="qp",
        )
        self.paper_econ = PastPaper.objects.create(
            metadata=self.metadata_econ,
            asset=self.asset,
            parsed_state="READY",
            is_active=True,
        )
        PastPaperComponent.objects.create(
            paper=self.paper_econ,
            num_display="Q1",
            content="Discuss inflation trends in an open economy.",
            page=1,
        )

        self.metadata_geo = PastPaperMetadata.objects.create(
            paper_code="8291_s19_qp_11",
            exam_board="CAIE",
            subject="Geography",
            syllabus_code="8291",
            season="Summer",
            year=2019,
            paper_type="qp",
        )
        self.paper_geo = PastPaper.objects.create(
            metadata=self.metadata_geo,
            asset=self.asset,
            parsed_state="READY",
            is_active=True,
        )
        PastPaperComponent.objects.create(
            paper=self.paper_geo,
            num_display="Q2",
            content="Explain how inflation affects demand in regional trade.",
            page=2,
        )

    def test_applies_metadata_scope_before_relax(self):
        query = KeywordQuery(query="inflation demand", subject="Economics", limit=5)
        results = search_components(query)
        self.assertEqual(1, len(results))
        self.assertEqual("9708_w20_qp_12", results[0].paper_code)

    def test_relaxes_scope_when_no_hits(self):
        query = KeywordQuery(query="inflation demand", subject="Philosophy", limit=5)
        results = search_components(query)
        self.assertEqual(2, len(results))
        paper_codes = sorted(r.paper_code for r in results)
        self.assertEqual(["8291_s19_qp_11", "9708_w20_qp_12"], paper_codes)

    def test_high_signal_tokens_prioritize_codes(self):
        query = KeywordQuery(query="9708 demand curve", limit=5)
        results = search_components(query)
        self.assertTrue(results)
        self.assertTrue(all(r.paper_code.startswith("9708") for r in results))
