import base64
import io
import json
from typing import List, Dict, Any, Optional, Union

import pdfplumber

from backend.apps.pastpaper.parsers.base import QNode, BaseParser

from openai import OpenAI, base_url

import os
import re
import json
from typing import List, Dict, Any, Optional
from openai import AsyncOpenAI


class JSONChatClient:
    def __init__(self,
                 api_key: Optional[str],
                 base_url: str):
        """
        Initialize the JSONChatClient.
        """
        self.client = AsyncOpenAI(
            api_key=api_key or os.environ.get("DEEPSEEK_APIKEY"),
            base_url=base_url
        )

    @staticmethod
    def extract_json_from_response(response_text: str) -> Dict[str, Any]:
        """
        Parse JSON from a model response string.
        """
        result = {"raw_data": response_text, "data": None, "error": None}
        try:
            result["data"] = json.loads(response_text)
        except json.JSONDecodeError as e:
            # fallback: try regex if extra text is around the JSON
            try:
                json_str_match = re.search(r"\{[\s\S]*\}", response_text)
                if json_str_match:
                    result["data"] = json.loads(json_str_match.group())
            except Exception:
                pass
            result["error"] = str(e)
        return result

    async def chat_json(
            self,
            messages: List[Dict[str, Any]],
            model: str,
            **kwargs
    ) -> Dict[str, Any]:
        """
        Asynchronously call the chat endpoint and return parsed JSON response.
        """
        response = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},  # array OR object allowed
            **kwargs,
        )
        print("Prompt tokens:", response.usage.prompt_tokens)
        print("Completion tokens:", response.usage.completion_tokens)
        print("Total tokens:", response.usage.total_tokens)

        content = response.choices[0].message.content
        return self.extract_json_from_response(content)


class LLMClient:
    """
    LLM client for parsing exam pages into structured question objects.

    Supports:
    - OCR text input
    - Page image input (base64 encoded)
    """

    def __init__(self, model: str = "deepseek-chat", api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.client = JSONChatClient(
            api_key=api_key,
            base_url=base_url,
        )
        self.model = model

    def _build_prompt(
            self,
            tree_context_json: str,
            last_node_json: str,
            page_text: str = "",
            input_type: str = "text",
    ) -> str:
        """Builds the user prompt, specialized for text vs image input."""
        base_context = f"""
Current tree context (simplified):
{tree_context_json}

Last parsed node:
{last_node_json}

Instructions:
For each question-like block on this page, create an object with this schema:
{{
  "num": string | null,
  "content": string,
  "marks": integer | null,
  "level": integer
}}
YOU MUST OUTPUT A VALID JSON ARRAY WITH NO EXTRA FIELDS OR TEXT
i.e. The Json should be [{{"num": ..., "content": ..., "marks": ..., "level": ...}}, ...]

Rules:
1. "num": extract ONLY the local question number token exactly as it appears at the start 
   of the question line. 
   - Examples:
     - If the paper shows "1", output "1".
     - If the paper shows "(a)", output "(a)" (NOT "1(a)").
     - If the paper shows "(ii)", output "(ii)" (NOT "1(a)(ii)").
     - If the paper shows "Q3", output "Q3".
     - If the text is a continuation without a number, set "num": null.
   Do not combine child numbers with their parents.
   Special Case — Overlapping Numbers: When nested numbering appears (e.g., 1(a)(i)), ensure that:
      - The actual question content is stored under the smallest unit ((i) in this example).
      - Parent nodes (1, (a)) are created as empty structural nodes to preserve hierarchy, but they must not hold repeated text.
    For any parts that appear to be sub-question of a parent question but lack explicit numbering, make suitable numbering by your logics to make the hierarchy clear.
    If there is a Either...Or question, do not make them sub-question but instead the content of one question.
2. "content": extract the full question text (including background, description, and all body parts), 
   but always exclude any explicit marks notation. 
   e.g. Valid content includes the description of the background of the question that is necessary for candidates to know.

3. "marks": extract the mark allocation if present, as an integer. 
   - Normally it appears in square brackets [x], e.g. "[4]" → 4. 
   - Sometimes it may appear in other notations such as "(4 marks)". 
   - If such alternative formats are clearly indicating marks, normalize them to the integer (e.g. "(4 marks)" → 4).
   - If a single question appear to have multiple mark allocations (e.g. "[2] ... [2]"), sum them up (→ 4).
   - If a question is in the format of Either / Or (e.g. "Either (a) or (b) [4]"), assign the marks to the parent question (→ 4) and set child parts to null.
   - If no clear mark allocation is present, set "marks": null.
4. "level": infer hierarchy step by step using tree context + last node:
   - Same sibling → same level
   - Child → last_node.level + 1
   - New top-level → 0
   - Digits often top-level, letters often sub, roman often sub-sub, but follow context.
   - If continuation ("num": null), inherit last_node.level.
   - Levels are not capped — use higher integers if nesting goes deeper.
5. Ignore page headers, footers, admin text. But don't exclude the texts that might be the parts of a question.
6. Output ONLY a JSON array.
"""

        if input_type == "text":
            return f"""
You are given OCR extracted text of a page from an exam paper (may contains inaccuracies because of noise)
Page text (OCR extracted):
\"\"\"{page_text}\"\"\"

{base_context}
"""
        elif input_type == "image":
            return f"""
You are given an image of a page from an exam paper.
Read the visible text in the image and fulfill your task described below.
If you encountered diagrams that is hard to express in text, just ignore them. Otherwise, try your best to extract all text.

Write all characters using standard Unicode representations. For mathematical or logical expressions, use plain-text formulas (e.g., x^2, 1/2) instead of special or non-standard symbols.
{base_context}
"""
        else:
            raise ValueError("input_type must be 'text' or 'image'")

    async def parse_page(
            self,
            page_input: Union[str, bytes],
            tree_context_json: str = "[]",
            last_node_json: str = "{}",
            input_type: str = "text"
    ) -> List[Dict[str, Any]]:
        """
        Parse a page with the LLM.
        """
        system_prompt = (
            "You are an exam paper parser.\n"
            "Your task is to read ONE PAGE of exam content and return a JSON array of question objects.\n"
            "Output must always be valid JSON with no explanations."
        )

        if input_type == "text":
            user_prompt = self._build_prompt(
                page_text=page_input,
                tree_context_json=tree_context_json,
                last_node_json=last_node_json,
                input_type="text",
            )
            response = await self.client.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model=self.model,
                temperature=0,
            )

        elif input_type == "image":
            if isinstance(page_input, bytes):
                page_input = base64.b64encode(page_input).decode("utf-8")

            user_prompt = self._build_prompt(
                tree_context_json=tree_context_json,
                last_node_json=last_node_json,
                input_type="image",
            )

            response = await self.client.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{page_input}"}}
                        ],
                    },
                ],
                reasoning_effort="minimal",
                model=self.model,
                temperature=0,
            )
        else:
            raise ValueError("input_type must be 'text' or 'image'")

        if response["error"]:
            raise RuntimeError(f"Failed to parse LLM JSON: {response['error']} | Raw: {response['raw_data']}")
        return response["data"]


class AgenticPaperTreeParser(BaseParser):
    """
    A past paper parser that uses LLM to extract structured
    question objects and then builds them into a tree of QNodes.
    Supports both text extraction and base64 image input.
    """

    def __init__(self, use_image: bool = False):
        self.use_image = use_image
        if self.use_image:
            self.llm = LLMClient(
                api_key=os.environ.get("OPENROUTER_APIKEY"),
                base_url="https://openrouter.ai/api/v1",
                # model="gpt-5-nano",
                model="qwen/qwen2.5-vl-72b-instruct:free",
            )
        else:
            self.llm = LLMClient(
                api_key=os.environ.get("DEEPSEEK_APIKEY"),
                base_url="https://api.deepseek.com",
                model="deepseek-chat"
            )

    async def parse_pdf(self, file_path: str) -> List[Dict[str, Any]]:
        root = QNode(num="ROOT", level=-1, norm="root")
        stack: List[QNode] = [root]

        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                if self.use_image:
                    # Render page as PNG and encode to base64
                    pil_img = page.to_image(resolution=200).original
                    buf = io.BytesIO()
                    pil_img.save(buf, format="PNG")
                    page_input = base64.b64encode(buf.getvalue()).decode("utf-8")
                    input_type = "image"
                else:
                    page_input = page.extract_text(x_tolerance=1) or ""
                    input_type = "text"

                # Serialize current tree + last node for LLM context
                tree_context_json = self._serialize_tree(root)
                last_node_json = stack[-1].to_dict() if len(stack) > 1 else "{}"

                # Ask LLM to parse current page
                questions = await self.llm.parse_page(
                    page_input=page_input,
                    tree_context_json=tree_context_json,
                    last_node_json=last_node_json,
                    input_type=input_type
                )

                # Process questions
                if isinstance(questions, dict):
                    questions = [questions]
                for q in questions:
                    num, content, marks, level = (
                        q.get("num"),
                        q.get("content") or "",
                        q.get("marks"),
                        q.get("level"),
                    )

                    if num is None:
                        # Continuation → append to last node
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

        return [c.to_dict() for c in root.children]

    def _anchor_parent_index(self, stack: List[QNode], level: int) -> int:
        i = len(stack) - 1
        while i >= 0 and stack[i].level >= level:
            i -= 1
        return i

    def _commit_reanchor(self, stack: List[QNode], parent_idx: int) -> QNode:
        while len(stack) - 1 > parent_idx:
            stack.pop()
        return stack[parent_idx]

    def _serialize_tree(self, root: QNode) -> str:
        def simplify(node: QNode):
            return {
                "num": node.num,
                "level": node.level,
                "score": node.score,
                "children": [simplify(ch) for ch in node.children],
            }

        return json.dumps([simplify(ch) for ch in root.children], ensure_ascii=False)


if __name__ == "__main__":
    import asyncio


    async def main():
        parser = AgenticPaperTreeParser(use_image=True)
        qtree = await parser.parse_pdf("./9231_w02_qp_2.pdf")
        print(json.dumps(qtree, ensure_ascii=False, indent=2))
        ...

    asyncio.run(main())
