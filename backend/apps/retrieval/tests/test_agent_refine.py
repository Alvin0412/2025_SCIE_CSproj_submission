from __future__ import annotations

import asyncio

from django.test import SimpleTestCase

from backend.apps.retrieval.agent import RetrievalAgent
from backend.apps.retrieval.services import QueryBlueprint


class _FakeLLMClient:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.model = "fake-model"

    async def complete_json(self, messages, *, model=None, temperature=None):
        self.messages = messages
        return self.payload


class BlueprintRefinerTests(SimpleTestCase):
    def setUp(self):
        self.base_blueprint = QueryBlueprint(
            raw_query="inflation in caie economics",
            subject="Economics",
            syllabus_code="9708",
            exam_board="CAIE",
            resource_type="question",
            year_range=(2018, 2020),
            keywords=("inflation",),
            semantic_seed="economics inflation",
            provenance={"intent_parser": "rule"},
        )
        self.snapshot = {"summary": {"total": 0, "sources": {}}, "top_candidates": []}

    def test_refine_blueprint_merges_updates(self):
        payload = {
            "action": "continue",
            "reason": "Focus on newer variants",
            "blueprint": {
                "year_range": [2021, 2023],
                "keywords": ["inflation", "monetary policy"],
                "subject": None,
            },
            "provenance": {"model": "fake"},
        }
        agent = RetrievalAgent(client=_FakeLLMClient(payload))
        revision = asyncio.run(agent.refine_blueprint(self.base_blueprint, self.snapshot))
        self.assertEqual("continue", revision.action)
        self.assertEqual((2021, 2023), revision.blueprint.year_range)
        self.assertEqual(("inflation", "monetary policy"), revision.blueprint.keywords)
        # Subject should be preserved since the LLM left it empty.
        self.assertEqual("Economics", revision.blueprint.subject)
        self.assertIn("refiner", revision.blueprint.provenance)

    def test_stop_action_respected(self):
        payload = {
            "action": "stop",
            "reason": "Workspace already saturated",
            "blueprint": {},
            "provenance": {},
        }
        agent = RetrievalAgent(client=_FakeLLMClient(payload))
        revision = asyncio.run(agent.refine_blueprint(self.base_blueprint, self.snapshot))
        self.assertEqual("stop", revision.action)
        self.assertEqual(self.base_blueprint.year_range, revision.blueprint.year_range)
        self.assertTrue(revision.provenance.get("prompt_version"))
