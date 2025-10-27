"""Background tasks for parsing past papers with the LLM pipeline."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

import dramatiq
from django.db import transaction
from django.utils import timezone

from backend.apps.pastpaper.models import PastPaper, PastPaperComponent
from backend.apps.pastpaper.parsers.llmparser import PaperParser
from backend.apps.pastpaper.parsers.prompts import PromptBuilder, BaseQPPromptBuilder, BaseMSPromptBuilder
from backend.apps.service.ioqueue.registry import io_task

logger = logging.getLogger(__name__)

PARSER_QUEUE = os.getenv("PASTPAPER_PARSER_QUEUE", "pastpaper-parse")
TEST_QUEUE = os.getenv("PASTPAPER_TEST_QUEUE", "pastpaper-test")
PARSER_TIME_LIMIT_MS = int(os.getenv("PASTPAPER_PARSER_TIME_LIMIT_MS", "600000"))
DEFAULT_USE_IMAGE = os.getenv("PASTPAPER_PARSER_USE_IMAGE", "false").lower() in {"1", "true", "yes"}


def trigger_parse_async(paper_id: str, version_no: int, *, use_image: Optional[bool] = None) -> None:
    """Enqueue the parsing task after the surrounding transaction commits."""

    def _enqueue() -> None:
        kwargs = {}
        effective_use_image = DEFAULT_USE_IMAGE if use_image is None else bool(use_image)
        if use_image is not None:
            kwargs["use_image"] = effective_use_image
        logger.info(
            "Queueing parse task",
            extra={
                "paper_id": paper_id,
                "version_no": version_no,
                "use_image": effective_use_image,
                "explicit": use_image is not None,
            },
        )
        parse_pastpaper.send(paper_id, version_no, **kwargs)

    try:
        transaction.on_commit(_enqueue)
    except RuntimeError:
        # No active transaction – enqueue immediately.
        logger.debug(
            "Trigger parse without transaction",
            extra={"paper_id": paper_id, "version_no": version_no},
        )
        _enqueue()


def trigger_test_task(message: str = "ping") -> str:
    """Enqueue a lightweight Dramatiq task for health checking."""

    logger.info("Queueing test task", extra={"task_message": message})
    run_test_task.send(message)


@io_task(persist=False)
def run_test_task(message: str) -> None:
    """No-op actor used to verify Dramatiq connectivity."""
    print("[run_test_task] message =", message)
    print("yeaaa")
    ...


def promptbuilder_factory(paper: PastPaper) -> type[PromptBuilder]:
    """
    Select the appropriate parser based on the paper's metadata.
    Right now the prompts are hard-coded in files.
    TODO: Later I will store prompts in db associating with suitable metadata and write logic to match from db.
    """
    if paper.metadata.paper_type in ("qp", "sq"):
        return BaseQPPromptBuilder
    if paper.metadata.paper_type in ("ms", "sm"):
        return BaseMSPromptBuilder
    else:  # default
        return BaseQPPromptBuilder


# @dramatiq.actor(queue_name=PARSER_QUEUE, max_retries=2, time_limit=PARSER_TIME_LIMIT_MS)
@io_task(persist=False, throttle_interval=0.5)
def parse_pastpaper(paper_id: str, version_no: int, use_image: Optional[bool] = None) -> None:
    """Parse a stored PDF and persist the extracted question tree."""

    logger.info("PastPaper parse requested: id=%s version=%s", paper_id, version_no)

    try:
        paper = (
            PastPaper.objects.select_related("asset", "metadata")
            .get(paper_id=paper_id, version_no=version_no)
        )
    except PastPaper.DoesNotExist:
        logger.warning("PastPaper not found: id=%s version=%s", paper_id, version_no)
        return

    PastPaper.objects.filter(pk=paper.pk).update(
        parsed_state="RUNNING",
        last_error="",
        updated_at=timezone.now(),
    )

    selected_use_image = DEFAULT_USE_IMAGE if use_image is None else bool(use_image)
    prompt_builder = promptbuilder_factory(paper)()

    parser = PaperParser(
        use_image=selected_use_image,
        prompt_builder=prompt_builder,
    )

    file_path = getattr(paper.asset.file, "path", None)
    if not file_path:
        raise FileNotFoundError("PastPaper asset file path is unavailable")

    try:
        parsed_tree = _run_parser(parser, file_path)
        _persist_tree(paper, parsed_tree)
        logger.info(
            "PastPaper parsed successfully: id=%s version=%s nodes=%s",
            paper_id,
            version_no,
            _count_nodes(parsed_tree),
        )
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        PastPaper.objects.filter(pk=paper.pk).update(
            parsed_state="ERROR",
            last_error=err[:2000],
            updated_at=timezone.now(),
        )
        logger.exception(
            "PastPaper parsing failed: id=%s version=%s error=%s",
            paper_id,
            version_no,
            err,
        )
        raise


def _run_parser(parser: PaperParser, file_path: str) -> list[dict[str, Any]]:
    """Run the async parser in a synchronous actor context."""

    async def _parse() -> list[dict[str, Any]]:
        return await parser.parse_pdf(file_path)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_parse())
    else:  # pragma: no cover
        return loop.run_until_complete(_parse())


def _persist_tree(paper: PastPaper, tree: list[dict[str, Any]]) -> None:
    """Store the parsed tree as JSON and denormalized components."""

    with transaction.atomic():
        locked = PastPaper.objects.select_for_update().get(pk=paper.pk)
        locked.parsed_tree = tree
        locked.parsed_state = "READY"
        locked.last_error = ""
        locked.save(update_fields=["parsed_tree", "parsed_state", "last_error", "updated_at"])

        locked.components.all().delete()
        _create_components(locked, tree)


def _create_components(paper: PastPaper, nodes: Iterable[dict[str, Any]],
                       parent: Optional[PastPaperComponent] = None) -> None:
    for node in nodes:
        component = PastPaperComponent.objects.create(
            paper=paper,
            parent=parent,
            num_display=(node.get("num") or "").strip(),
            content=(node.get("content") or "").strip(),
            score=_to_decimal(node.get("score")),
            page=node.get("page"),
            position=node.get("position"),
        )
        children = node.get("children") or []
        if children:
            _create_components(paper, children, component)


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _count_nodes(tree: Iterable[dict[str, Any]]) -> int:
    total = 0
    stack = list(tree)
    while stack:
        node = stack.pop()
        total += 1
        stack.extend(node.get("children") or [])
    return total
