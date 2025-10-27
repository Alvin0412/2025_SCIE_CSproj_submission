import asyncio
import base64
import io
import json
import os
import re
import functools
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Literal, Callable

import httpx
import pdfplumber
from openai import AsyncOpenAI
from async_lru import alru_cache

from backend.apps.pastpaper.parsers.base import QNode, BaseParser
from backend.apps.pastpaper.parsers.prompts import PromptBuilder, BaseQPPromptBuilder, RevisedQPPromptBuilder

logger = logging.getLogger(__name__)


def async_ttl_cache(ttl: float = 300.0):
    """
    缓存协程函数返回值一段时间 (TTL 秒)，过期后重新计算。
    支持不可哈希参数，会自动降级为 (type_name, id(obj))。
    """
    cache: Dict[Tuple[Any, ...], Tuple[float, Any]] = {}
    lock = asyncio.Lock()

    def make_hashable(x: Any):
        """尝试生成可哈希表示"""
        try:
            hash(x)
            return x
        except TypeError:
            # 尝试对常见容器类型递归处理
            if isinstance(x, dict):
                return tuple(sorted((make_hashable(k), make_hashable(v)) for k, v in x.items()))
            elif isinstance(x, (list, set, tuple)):
                return tuple(make_hashable(i) for i in x)
            elif hasattr(x, "__dict__"):  # 自定义类实例
                return (x.__class__.__name__, id(x))
            else:
                return (str(type(x)), id(x))

    def make_key(args, kwargs):
        """统一生成缓存 key"""
        safe_args = tuple(make_hashable(a) for a in args)
        safe_kwargs = tuple(sorted((k, make_hashable(v)) for k, v in kwargs.items()))
        return (safe_args, safe_kwargs)

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            key = make_key(args, kwargs)
            now = time.time()

            async with lock:
                if key in cache:
                    expire_ts, value = cache[key]
                    if now < expire_ts:
                        return value

            # 缓存过期或不存在
            result = await func(*args, **kwargs)

            async with lock:
                cache[key] = (now + ttl, result)

            return result

        return wrapper

    return decorator


@dataclass(frozen=True)
class ProviderTransportConfig:
    base_url: str
    api_key_env: str
    timeout_sec: float = 45.0
    max_retries: int = 3
    initial_backoff_sec: float = 0.5
    concurrency: int = 8
    force_json: bool = True
    default_temperature: float = 0.0
    model_ids: Optional[List[str]] = None
    provider_client: "ProviderClient" = None

    @property
    def api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise RuntimeError(f"Missing API key: set {self.api_key_env}")
        return key


class JsonExtractor:
    CODEFENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)

    @classmethod
    def extract(cls, text: str) -> Dict[str, Any]:
        raw = (text or "").strip()
        errors: List[str] = []

        data, err = cls._try_json(raw)
        if data is not None:
            return {"raw_data": text, "data": data, "error": None}
        if err:
            errors.append(f"Top-level JSON: {err}")

        for m in cls.CODEFENCE_RE.finditer(raw):
            block = m.group(1)
            data, err = cls._try_json(block)
            if data is not None:
                return {"raw_data": text, "data": data, "error": None}
            if err:
                errors.append(f"Code block: {err}")

        data = cls._scan_balanced_json(raw)
        if data is not None:
            return {"raw_data": text, "data": data, "error": None}

        errors.append("Balanced scan: No valid JSON found")
        return {"raw_data": text, "data": None, "error": " | ".join(errors)}

    @staticmethod
    def _try_json(blob: str):
        try:
            return json.loads(blob), None
        except json.JSONDecodeError as e:
            return None, str(e)

    @staticmethod
    def _scan_balanced_json(s: str):
        for open_ch, close_ch in [("{", "}"), ("[", "]")]:
            starts = [i for i, ch in enumerate(s) if ch == open_ch]
            for start in starts:
                depth = 0
                for i in range(start, len(s)):
                    if s[i] == open_ch:
                        depth += 1
                    elif s[i] == close_ch:
                        depth -= 1
                        if depth == 0:
                            snippet = s[start:i + 1]
                            try:
                                return json.loads(snippet)
                            except json.JSONDecodeError:
                                pass
        return None


# =========================
# Transport：OpenAI SDK（必须传 model）
# =========================
class AsyncLLM:
    def __init__(self, cfg: ProviderTransportConfig):
        self.cfg = cfg
        self.client = AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
        self.sem = asyncio.Semaphore(cfg.concurrency)

    async def chat(self, *, model: str, messages, temperature: Optional[float] = None,
                   extra_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": self.cfg.default_temperature if temperature is None else temperature,
        }
        if self.cfg.force_json:
            params["response_format"] = {"type": "json_object"}
        if extra_params:
            params.update(extra_params)

        backoff = self.cfg.initial_backoff_sec
        for attempt in range(self.cfg.max_retries + 1):
            async with self.sem:
                try:
                    coro = self.client.chat.completions.create(**params)
                    resp = await asyncio.wait_for(coro, timeout=self.cfg.timeout_sec)
                    try:
                        logger.debug(
                            "model=%s prompt=%s completion=%s total=%s",
                            model,
                            getattr(resp.usage, "prompt_tokens", None),
                            getattr(resp.usage, "completion_tokens", None),
                            getattr(resp.usage, "total_tokens", None),
                        )
                    except Exception:
                        pass
                    return {
                        "text": resp.choices[0].message.content or "",
                        "usage": getattr(resp, "usage", None),
                    }
                except Exception as e:
                    # 网络/429/timeout 均走指数退避；最终抛给上层做 fallback
                    if attempt < self.cfg.max_retries:
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    raise e


class JSONChatClient:
    """只做“请求 + JSON 解析”，不掺和模型选择。"""

    def __init__(self, cfg: ProviderTransportConfig):
        self.cfg = cfg
        self.transport = AsyncLLM(cfg)

    async def chat_json(self, *, model: str, messages, temperature: Optional[float] = None,
                        extra_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raw = ""
        try:
            resp = await self.transport.chat(model=model, messages=messages, temperature=temperature,
                                             extra_params=extra_params)
            raw = resp.get("text", "") or ""
            parsed = JsonExtractor.extract(raw)
            return parsed
        except Exception as e:
            return {"raw_data": raw, "data": None, "error": str(e)}


# =========================
# OpenRouter /models 客户端（只取候选）
# =========================
class ProviderClient:
    async def get_models(self, model_ids: List[str]) -> List[Dict[str, Any]]:
        raise NotImplementedError


class OpenRouterClient(ProviderClient):
    def __init__(self,
                 api_key_env: str = "OPENROUTER_APIKEY",
                 base_url: str = "https://openrouter.ai/api/v1",
                 ):
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise RuntimeError(f"Missing API key: set {api_key_env}")
        self._api_key = api_key
        self._base_url = base_url

    @async_ttl_cache(ttl=300.0)
    async def get_models(self, model_ids: List[str]) -> List[Dict[str, Any]]:
        url = f"{self._base_url}/models"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                raise RuntimeError(f"OpenRouter /models error {r.status_code}: {r.text}")
            data = r.json()
            all_models = data.get("data", [])
            return [m for m in all_models if m.get("id") in model_ids]


# =========================
# ModelSelector：单 provider，免费优先，失败冷却
# =========================
class ModelSelector:
    def __init__(self, client: ProviderClient, candidate_ids: List[str], need_image: bool = False,
                 cooldown_sec: int = 300):
        self.client = client
        self.candidate_ids = candidate_ids
        self.need_image = need_image
        self.cooldown_sec = cooldown_sec
        self._unavailable_until: Dict[str, float] = {}  # model_id -> ts

    async def select(self) -> Dict[str, Any]:
        models = await self.client.get_models(self.candidate_ids)
        now = time.time()

        def available(m: Dict[str, Any]) -> bool:
            mid = m.get("id", "")
            if not mid:
                return False
            ts = self._unavailable_until.get(mid)
            if ts is not None and now < ts:
                return False
            if self.need_image:
                caps = m.get("architecture", {}).get("input_modalities", ["text"])
                if "image" not in caps:
                    return False
            return True

        valid = [m for m in models if available(m)]
        if not valid:
            raise RuntimeError("No valid OpenRouter models after filtering/unavailable cooldown.")

        # 免费优先（prompt 单价为 0），否则按 completion 单价排序
        def price_tuple(model: Dict[str, Any]) -> Tuple[float, float]:
            pricing = model.get("pricing") or {}
            return float(pricing.get("prompt", float("inf"))), float(pricing.get("completion", float("inf")))

        free = [m for m in valid if price_tuple(m)[0] == 0.0]
        if free:
            return min(free, key=lambda m: price_tuple(m)[1])
        return min(valid, key=lambda m: (price_tuple(m)[1], price_tuple(m)[0]))

    def cooldown(self, model_id: str, seconds: Optional[int] = None):
        sec = self.cooldown_sec if seconds is None else seconds
        self._unavailable_until[model_id] = time.time() + max(1, sec)


InputType = Literal["text", "image"]
OpenRouterConfig = ProviderTransportConfig(
    base_url="https://openrouter.ai/api/v1",
    api_key_env="OPENROUTER_APIKEY",
    timeout_sec=45.0,
    max_retries=3,
    initial_backoff_sec=0.5,
    concurrency=8,
    force_json=True,
    default_temperature=0.0,
    model_ids=[
        # "qwen/qwen2.5-vl-72b-instruct",
        "google/gemini-2.0-flash-001",
        # "qwen/qwen2.5-vl-72b-instruct:free",
        # "microsoft/phi-4-multimodal-instruct",
    ],
    provider_client=OpenRouterClient()
)


class PaperParser(BaseParser):
    def __init__(
            self,
            use_image: bool = False,
            provider_cfg: Optional[ProviderTransportConfig] = OpenRouterConfig,
            model_cooldown_sec: int = 300,
            prompt_builder: PromptBuilder = None,
    ):
        self.use_image = use_image

        self.provider_cfg = provider_cfg
        self.llm = JSONChatClient(self.provider_cfg)

        # Prompt stub
        self.prompt_builder = PromptBuilder() if prompt_builder is None else prompt_builder

        self.selector = ModelSelector(
            client=self.provider_cfg.provider_client,
            candidate_ids=self.provider_cfg.model_ids,
            need_image=use_image,
            cooldown_sec=model_cooldown_sec,
        )

    async def parse_pdf(self, file_path: str):
        root = QNode(num="ROOT", level=-1, norm="root")
        stack = [root]
        all_resp = []
        try:
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    page_input, input_type = await self._page_input(page, self.use_image)
                    tree_json = self._serialize_tree(root)
                    last_node_json = stack[-1].to_dict() if len(stack) > 1 else "{}"
                    messages, extra = self.prompt_builder.build(page_input, input_type, tree_json, last_node_json)

                    parsed = None
                    errors: List[str] = []
                    tried: set = set()

                    for _ in range(len(self.selector.candidate_ids)):
                        model_info: Dict[str, Any]
                        model_id: str
                        try:
                            model_info = await self.selector.select()
                            model_id = model_info["id"]
                            if model_id in tried:
                                continue
                            tried.add(model_id)  # ?

                            resp = await self.llm.chat_json(model=model_id, messages=messages, extra_params=extra)
                            all_resp.append(resp)
                            if resp["data"] is not None:
                                parsed = resp["data"]
                                break

                            # 解析失败：冷却该模型，换下一个
                            # self.selector.cooldown(model_id)
                            errors.append(f"[{model_id}] parse_error={resp['error']}")
                            if resp.get("error") and "free-models-per-day-high-balance" in resp["error"]:
                                logger.info("Model %s reached daily limit.", model_id)
                                self.selector.cooldown(model_id, seconds=60*60) # retry after 1h to see if released
                            if resp.get("error") and "429" in resp["error"]:
                                logger.info("Model %s hit rate limit, cooling down.", model_id)
                                self.selector.cooldown(model_id)
                            elif resp.get("error") and any(tok in resp["error"] for tok in
                                                        ["timed out", "timeout", "timedout", "time out"]):
                                logger.info("Model %s timeout, cooling down.", model_id)
                                self.selector.cooldown(model_id, seconds=60)
                            elif resp.get("error") and  "403" in resp["error"]:
                                logger.info("Model %s forbidden (403) or key limit exceeded, cooling down.", model_id)
                                self.selector.cooldown(model_id, seconds=600)
                        except Exception as e:
                            # 请求层异常：冷却并换下一个
                            emsg = str(e)
                            errors.append(f"[{locals().get('model_id', '?')}] call_failed={emsg}")
                            mid = locals().get("model_id")
                            if mid and "429" in emsg:
                                logger.info("Model %s hit rate limit, cooling down.", mid)
                                self.selector.cooldown(mid)
                            continue

                    if parsed is None:
                        logger.error("All candidate models failed. %s", errors)
                        raise RuntimeError("No model produced valid JSON for this page.")

                    # 构树
                    questions = parsed if isinstance(parsed, list) else [parsed]
                    for q in questions:
                        num = q.get("num")
                        content = q.get("content") or ""
                        marks = q.get("marks")
                        level = q.get("level")

                        if not num or num == stack[-1].num:  # 如果是承接上个节点
                            if stack and stack[-1].level >= 0:
                                stack[-1].add_text(content)
                                if marks is not None:
                                    stack[-1].score = marks
                            continue

                        parent_idx = self._anchor_parent_index(stack, level)
                        parent = self._commit_reanchor(stack, parent_idx)
                        node = QNode(num=num, level=level, norm=num)
                        node.add_text(content)
                        if marks is not None:
                            node.score = marks
                        parent.children.append(node)
                        stack.append(node)
        finally:
            # 🧹确保在loop关闭前关闭openai的AsyncClient
            try:
                await self.llm.transport.client.close()
            except RuntimeError as e:
                if "Event loop is closed" not in str(e):
                    raise
        ...
        return [c.to_dict() for c in root.children]


    async def _page_input(self, page, image: bool):
        if image:
            buf = io.BytesIO()
            page.to_image(resolution=200).original.save(buf, format="PNG")
            return buf.getvalue(), "image"
        return page.extract_text(x_tolerance=1) or "", "text"

    def _anchor_parent_index(self, stack, level):
        i = len(stack) - 1
        while i >= 0 and stack[i].level >= level:
            i -= 1
        return i

    def _commit_reanchor(self, stack, parent_idx):
        while len(stack) - 1 > parent_idx:
            stack.pop()
        return stack[parent_idx]

    def _serialize_tree(self, root):
        def simplify(node):
            return {"num": node.num, "level": node.level, "score": node.score,
                    "children": [simplify(ch) for ch in node.children]}

        return json.dumps([simplify(ch) for ch in root.children], ensure_ascii=False)


if __name__ == "__main__":
    async def main():
        pb = RevisedQPPromptBuilder()
        parser = PaperParser(use_image=True,prompt_builder=pb)
        # qtree = await parser.parse_pdf("./9708_w16_qp_42.pdf")
        qtree = await parser.parse_pdf("./9708_s15_qp_11.pdf")
        print(json.dumps(qtree, ensure_ascii=False, indent=2))
        ...


    asyncio.run(main())
