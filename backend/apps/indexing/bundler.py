"""Utilities to convert PastPaper component trees into semantic bundles."""
from __future__ import annotations
import logging

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List

from backend.apps.pastpaper.models import PastPaper, PastPaperComponent

from .tokenization import TokenizerProtocol, count_tokens

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BundleSpec:
    sequence: int
    component_ids: List[int]
    span_paths: List[str]
    text: str
    token_count: int
    title: str


@dataclass(slots=True)
class _SubtreePayload:
    ids: List[int]
    paths: List[str]
    texts: List[str]
    tokens: int


def build_bundles(
    paper: PastPaper,
    *,
    tokenizer: TokenizerProtocol,
    target_tokens: int,
) -> list[BundleSpec]:
    """Return bundle specifications respecting the approximate token budget."""

    components = list(
        paper.components.select_related("parent").order_by("depth", "path_normalized", "id")
    )
    if not components:
        return []

    children: Dict[int | None, List[PastPaperComponent]] = defaultdict(list)
    for comp in components:
        children[comp.parent_id].append(comp)

    for siblings in children.values():
        siblings.sort(key=_component_sort_key)

    path_map = {comp.id: comp.path_normalized or comp.num_display or "" for comp in components}
    text_map = {comp.id: _component_text(comp) for comp in components}
    token_map = {comp.id: count_tokens(text_map[comp.id], tokenizer) for comp in components}

    subtree_cache: Dict[int, _SubtreePayload] = {}

    def compute_subtree(component: PastPaperComponent) -> _SubtreePayload:
        cached = subtree_cache.get(component.id)
        if cached:
            return cached

        ids = [component.id]
        paths = [path_map[component.id]]
        texts = [text_map[component.id]] if text_map[component.id] else []
        tokens = token_map[component.id]

        for child in children.get(component.id, []):
            payload = compute_subtree(child)
            ids.extend(payload.ids)
            paths.extend(payload.paths)
            texts.extend(payload.texts)
            tokens += payload.tokens

        payload = _SubtreePayload(ids=ids, paths=paths, texts=texts, tokens=tokens)
        subtree_cache[component.id] = payload
        return payload

    def compose_bundle(payload: _SubtreePayload) -> Dict[str, object]:
        text = "\n\n".join(payload.texts)
        return {
            "component_ids": payload.ids.copy(),
            "span_paths": payload.paths.copy(),
            "text": text,
            "token_count": payload.tokens,
            "title": _bundle_title(payload.paths, payload.texts),
        }

    target = max(1, target_tokens)

    def bundle_node(component: PastPaperComponent) -> List[Dict[str, object]]:
        payload = compute_subtree(component)
        children_nodes = children.get(component.id, [])
        if payload.tokens <= target or not children_nodes:
            return [compose_bundle(payload)] if payload.texts else []

        bundles: List[Dict[str, object]] = []

        current_ids: List[int] = [component.id]
        current_paths: List[str] = [path_map[component.id]]
        current_texts: List[str] = []
        if text_map[component.id]:
            current_texts.append(text_map[component.id])
        current_tokens = token_map[component.id]

        def flush(force: bool = False) -> None:
            nonlocal current_ids, current_paths, current_texts, current_tokens
            if not current_ids:
                return
            if current_texts or force:
                bundle_payload = _SubtreePayload(
                    ids=current_ids.copy(),
                    paths=current_paths.copy(),
                    texts=current_texts.copy(),
                    tokens=current_tokens,
                )
                bundle = compose_bundle(bundle_payload)
                bundles.append(bundle)
            current_ids = []
            current_paths = []
            current_texts = []
            current_tokens = 0

        if current_tokens > target:
            flush(force=True)

        for child in children_nodes:
            child_payload = compute_subtree(child)

            if child_payload.tokens > target:
                flush()
                bundles.extend(bundle_node(child))
                continue

            prospective_tokens = current_tokens + child_payload.tokens if current_ids else child_payload.tokens
            if current_ids and current_tokens and prospective_tokens > target:
                flush()

            if not current_ids:
                current_tokens = 0

            current_ids.extend(child_payload.ids)
            current_paths.extend(child_payload.paths)
            current_texts.extend(child_payload.texts)
            current_tokens += child_payload.tokens

        flush()
        return bundles

    root_components = children.get(None, [])
    root_components.sort(key=_component_sort_key)

    bundle_dicts: List[Dict[str, object]] = []
    for root in root_components:
        bundle_dicts.extend(bundle_node(root))

    results: list[BundleSpec] = []
    for sequence, payload in enumerate(bundle_dicts, start=1):
        results.append(
            BundleSpec(
                sequence=sequence,
                component_ids=payload["component_ids"],
                span_paths=payload["span_paths"],
                text=payload["text"],
                token_count=payload["token_count"],
                title=payload["title"],
            )
        )
    for i, bundle in enumerate(results, start=1):
        logger.info("Bundle %d: tokens=%d, components=%d, title=%r", i, bundle.token_count, len(bundle.component_ids), bundle.title)
    return results


def _component_sort_key(component: PastPaperComponent) -> tuple[str, int]:
    return (component.path_normalized or component.num_display or "", component.id)


def _component_text(component: PastPaperComponent) -> str:
    parts: list[str] = []
    label = (component.num_display or "").strip()
    content = (component.content or "").strip()
    if label:
        parts.append(label)
    if content:
        if parts:
            parts[-1] = f"{parts[-1]}: {content}"
        else:
            parts.append(content)
    return parts[0] if parts else ""


def _bundle_title(paths: List[str], texts: List[str]) -> str:
    if not paths:
        return ""
    if texts:
        first_line = texts[0].splitlines()[0]
        if len(first_line) > 96:
            first_line = first_line[:93].rstrip() + "..."
        return first_line
    return paths[0]
