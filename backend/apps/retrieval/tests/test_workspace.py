from __future__ import annotations

from django.test import SimpleTestCase

from backend.apps.retrieval.services import SearchWorkspace, WorkspaceCandidate


class SearchWorkspaceSnapshotTests(SimpleTestCase):
    def test_snapshot_reports_top_candidates(self):
        workspace = SearchWorkspace()
        workspace.add_candidates(
            [
                WorkspaceCandidate(
                    candidate_id="component:1",
                    paper_uuid="uuid-1",
                    paper_code="9708_w20_qp_12",
                    year=2020,
                    path="Q1",
                    snippet="Discuss inflation",
                    score=0.8,
                    source="pastpaper_keyword",
                    subject="Economics",
                    syllabus_code="9708",
                    exam_board="CAIE",
                ),
                WorkspaceCandidate(
                    candidate_id="chunk:xyz",
                    paper_uuid="uuid-2",
                    paper_code="9709_s21_qp_42",
                    year=2021,
                    path="Section B",
                    snippet="Graph theory chunk",
                    score=0.9,
                    source="qdrant_semantic",
                    subject="Mathematics",
                    syllabus_code="9709",
                    exam_board="CAIE",
                ),
            ]
        )
        snapshot = workspace.snapshot(limit=1)
        self.assertEqual(snapshot["summary"]["total"], 2)
        self.assertEqual(snapshot["summary"]["sources"]["pastpaper_keyword"], 1)
        self.assertEqual(len(snapshot["top_candidates"]), 1)
        self.assertEqual(snapshot["top_candidates"][0]["paper_code"], "9709_s21_qp_42")
