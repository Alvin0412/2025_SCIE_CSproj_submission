import re
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Iterable
import pdfplumber

from backend.apps.pastpaper.parsers.base import QNode, BaseParser


class CAIEPaperTreeParser(BaseParser):
    """
    树形解析器：
      层级： 数字(1,2,...) -> level 0
             字母(a,b,...) -> level 1
             罗马(i,ii,iii,...) -> level 2
    每次识别到题号链，就按层级依次打开/定位节点；后续文本行附加到当前节点。
    在任一节点文本中匹配到 [score] 即记录到该节点。
    """

    # 题号 token：支持 1/(1)/1)/1. ; a/(a)/a)/a. ; iii/(iii)/iii)/iii.
    # TOKEN_RE = re.compile(
    #     r"""
    #     ^\s*(
    #         \(\d+\)|\d+[.)]|\d+      |      # 数字
    #         \([a-z]\)|[a-z][.)]|[a-z]|      # 字母
    #         \([ivx]+\)|[ivx]+[.)]|[ivx]+    # 罗马
    #     )
    #     (?=\s|$)                            # 裸形式右侧需空白或行尾
    #     """, re.IGNORECASE | re.VERBOSE
    # )
    TOKEN_RE = re.compile(
        r"""
        ^\s*(
            \(\d+\)|\d+[.)]|\d+(?=\s|$)        |   # 数字
            \([a-z]\)                          |   # 字母（必须带括号）
            \([ivx]+\)|[ivx]+[.)]|[ivx]+(?=\s|$)   # 罗马
        )
        """, re.IGNORECASE | re.VERBOSE
    )
    SCORE_RE = re.compile(r"\[\s*(\d+)\s*\]")

    def __init__(self):
        self.UCLES_RE = re.compile(r"^\s*©\s*UCLES\s+\d{4}\s+\S+(?:/\S+)+", re.IGNORECASE)
        # self.NOISE_RE = re.compile(
        #     r"(reasonable effort has been made by the publisher|publisher will be pleased to make amends at the earliest possible opportunity|all copyright acknowledgements are reproduced online in the Cambridge|www.cambridgeinternational.org|Assessment International Education Copyright|Permission to reproduce items|Cambridge Assessment International Education|Local Examinations Syndicate|Specimen|Syllabus|Marks?)",
        #     re.IGNORECASE,
        # )
        self.COPYBLOCK_RE = re.compile(
            r"(reasonable effort has been made by the publisher|"
            r"publisher will be pleased to make amends at the earliest possible opportunity|"
            r"all copyright acknowledgements are reproduced online in the Cambridge|"
            r"www\.cambridgeinternational\.org|"
            r"Assessment International Education Copyright|"
            r"Permission to reproduce items|"
            r"Cambridge Assessment International Education is part of Cambridge Assessment|"
            r"Answer one question from one section only|"
            r"Local Examinations Syndicate)",
            re.IGNORECASE
        )
        self.DURATION_RE = re.compile(
            r"^\s*\d+\s*hour[s]?(?:\s+\d+\s*minute[s]?)?\b", re.IGNORECASE
        )
        self.ADMIN_HEAD_RE = re.compile(
            r"^\s*(INSTRUCTIONS|INFORMATION|READ\s+THESE\s+INSTRUCTIONS|READ\s+THE\s+INFORMATION)\b",
            re.IGNORECASE,
        )
        self.ADMIN_LINE_RE = re.compile(
            r"(You must answer|You will need|Follow the instructions|Write your|Do not write|"
            r"handwriting|blue or black ink|additional answer paper|continuation booklet|"
            r"Total mark|Turn over|BLANK PAGE|This document has|Answer all questions)",
            re.IGNORECASE,
        )
        # self.SECTION_RE = re.compile(r"^\s*Section\s+[A-Z]\s*:", re.IGNORECASE)
        self.CODELINE_RE = re.compile(r"^\s*DC\s*\(.*?\)\s*\S+(?:/\S+)+\s*$", re.IGNORECASE)

        self.node_min_words = 5
        self.max_level = 2

    @staticmethod
    def _roman_to_int(s: str) -> int:
        roman_map = {'i': 1, 'v': 5, 'x': 10}
        s = s.lower()
        total, prev = 0, 0
        for ch in reversed(s):
            val = roman_map.get(ch, 0)
            if val < prev:
                total -= val
            else:
                total += val
            prev = val
        return total

    def _start_norm_for_level(self, level: int) -> str:
        return '1' if level == 0 else ('a' if level == 1 else 'i')

    def _is_next_norm(self, curr: str, prev: str, level: int) -> bool:
        if level == 0:
            try:
                return int(curr) == int(prev) + 1
            except ValueError:
                return False
        if level == 1:
            return len(curr) == 1 and len(prev) == 1 and ord(curr) == ord(prev) + 1
        if level == 2:
            return self._roman_to_int(curr) == self._roman_to_int(prev) + 1
        return False

    def _is_token_valid(self, token: dict[str, str], stack: List[QNode]) -> bool:
        """基于当前 stack 判断 token 是否可作为“下一个逻辑节点”。
        规则：
          - 计算出应当挂载的 parent（把 >= token.level 的都弹出后的栈顶）
          - 若该层还没有 sibling：必须是 parent 的下一层，且编号为该层起始值（'1'/'a'/'i'）
          - 若已有 sibling：必须是“最后一个 sibling 的下一个”
        """
        level = self._level_of(token["norm"])
        i = len(stack) - 1
        while i >= 0 and stack[i].level >= level:
            i -= 1
        if i < 0:
            return False

        parent = stack[i]
        siblings = [ch for ch in parent.children if ch.level == level]

        if not siblings:
            if level != parent.level + 1:
                return False
            # if parent.level != -1 and not self._node_has_score(parent):
            #     return False
            return token["norm"] == self._start_norm_for_level(level)

        prev = siblings[-1]
        if not self._node_has_score(prev):
            return False
        return self._is_next_norm(token["norm"], prev.norm, level)

    def _anchor_parent_index(self, stack: List[QNode], level: int) -> int:
        i = len(stack) - 1
        while i >= 0 and stack[i].level >= level:
            i -= 1
        return i

    def _commit_reanchor(self, stack: List[QNode], parent_idx: int) -> QNode:
        while len(stack) - 1 > parent_idx:
            stack.pop()
        return stack[parent_idx]

    def parse_lines(self, lines: Iterable[str]) -> List[Dict[str, Any]]:
        root = QNode(num="ROOT", level=-1, norm="root")
        stack: List[QNode] = [root]
        parent = root
        for raw in lines:
            line = raw.rstrip()
            if self._is_noise(line):
                continue

            tokens, rest = self._consume_chain(line)

            if tokens:
                for t in tokens:
                    level = self._level_of(t["norm"])
                    parent_idx = self._anchor_parent_index(stack, level)
                    if parent_idx < 0:
                        stack[-1].add_text(rest or "")
                        continue
                    parent = stack[parent_idx]

                    if not self._is_token_valid(t, stack):
                        stack[-1].add_text(line)
                        continue

                    parent = self._commit_reanchor(stack, parent_idx)
                    node = QNode(num=t["display"], level=level, norm=t["norm"])
                    parent.children.append(node)
                    stack.append(node)
                before, hit, after = self._split_by_score_keep_marker(rest)
                if before.strip():
                    stack[-1].add_text(before)
                if hit:
                    try:
                        n = int(self.SCORE_RE.search(before).group(1))
                        stack[-1].score = n
                    except Exception:
                        pass
                if after.strip():
                    stack[-1].add_text(after)
            else:
                before, hit, after = self._split_by_score_keep_marker(line)
                if before.strip():
                    stack[-1].add_text(before)
                if hit:
                    try:
                        n = int(self.SCORE_RE.search(before).group(1))
                        stack[-1].score = n
                    except Exception:
                        pass
                if after.strip():
                    stack[-1].add_text(after)

        tops = [c for c in root.children]
        tops = self._post_process_context(tops, min_words=self.node_min_words)
        return [c.to_dict() for c in tops]

    def _is_noise(self, line: str) -> bool:
        if not line.strip():
            return True
        # re_page_number = re.compile(r'^\s*\d{1,4}\s*$')
        if re.fullmatch(r"[\.\-–—\s]+", line):
            return True

        if re.match(r"^\s*(Working\s+space)\b", line, re.IGNORECASE):
            return True
        # if re_page_number.match(line): return True
        if self.UCLES_RE.match(line): return True
        if self.COPYBLOCK_RE.search(line): return True
        if self.DURATION_RE.match(line): return True  # “1 hour 15 minutes”
        if self.ADMIN_HEAD_RE.match(line): return True  # “INSTRUCTIONS/INFORMATION”
        # if self.SECTION_RE.match(line): return True  # “Section A: …”
        if self.CODELINE_RE.match(line): return True  # “DC (CE) 302835/2”
        if self.ADMIN_LINE_RE.search(line): return True  # “You must answer…” 等
        if re.fullmatch(r"\s*\d+\s*/\s*\d+\s*", line): return True  # “3 / 12”
        # 有些页脚把年份顶在行首，避免把 2022 当题号
        if re.match(r"^\s*20\d{2}\b", line): return True

        return False

    def _consume_chain(self, line: str):
        idx = 0
        n = len(line)
        tokens = []

        while idx < n:
            m = self.TOKEN_RE.match(line[idx:])
            if not m:
                break
            tok = m.group(1)
            display, norm = self._display_and_norm(tok)
            tokens.append({"display": display, "norm": norm})
            idx += m.end()
            while idx < n and line[idx] in " \t":
                idx += 1
            if not self.TOKEN_RE.match(line[idx:] or ""):
                break
        rest = line[idx:]
        return tokens, rest

    @staticmethod
    def _display_and_norm(tok: str):
        t = tok.strip()
        lower = t.lower()
        norm = lower
        if norm.startswith("(") and norm.endswith(")"):
            norm = norm[1:-1]
        norm = norm.rstrip(").")
        if norm.isdigit():
            display = norm
        elif re.fullmatch(r"[a-z]", norm):
            display = f"({norm})"
        else:
            display = f"({norm})"
        return display, norm

    def _level_of(self, norm: str) -> int:
        if norm.isdigit(): return 0
        if self._roman_to_int(norm) != 0:
            return 2
        if re.fullmatch(r"[a-z]", norm): return 1
        return 2

    def _maybe_set_score(self, node: QNode, text: str):
        for m in self.SCORE_RE.finditer(text or ""):
            try:
                node.score = int(m.group(1))
            except ValueError:
                pass

    def parse_pdf(self, pdf_path: str) -> List[Dict[str, Any]]:
        if pdfplumber is None:
            raise RuntimeError("pdfplumber 未安装：请先 pip install pdfplumber，或改用 parse_lines。")
        lines: List[str] = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text(x_tolerance=1) or ""
                lines.extend(text.splitlines())
        # print(lines)
        return self.parse_lines(lines)

    def _maybe_set_score_and_truncate(self, node: QNode, text: str) -> str:
        m = self.SCORE_RE.search(text or "")
        if m:
            try:
                node.score = int(m.group(1))
            except ValueError:
                pass
            return text[:m.end()].rstrip()
        return text

    def _node_has_score(self, node: QNode) -> bool:
        if node.score is not None:
            return True
        if not node.children:
            return False
        return all(self._node_has_score(ch) for ch in node.children)

    def _post_process_context(self, roots: List[QNode], min_words: int = 0) -> List[QNode]:
        word_re = re.compile(r"\w+", flags=re.UNICODE)

        def word_count(node: QNode) -> int:
            if not node.text_lines:
                return 0
            return len(word_re.findall("\n".join(node.text_lines)))

        def prune(node: QNode) -> (bool, bool):
            has_score_here = node.score is not None
            kept_children: List[QNode] = []
            any_child_has_score = False

            for ch in node.children:
                keep_child, child_has_score = prune(ch)
                if keep_child:
                    kept_children.append(ch)
                any_child_has_score = any_child_has_score or child_has_score

            node.children = kept_children
            has_score_subtree = has_score_here or any_child_has_score

            wc = word_count(node)
            is_leaf = (len(node.children) == 0)

            if is_leaf and wc < min_words and not has_score_here:
                return False, has_score_subtree

            if is_leaf and not has_score_here and wc < min_words:
                return False, has_score_subtree

            return True, has_score_subtree

        kept_roots: List[QNode] = []
        roots_score_flags: List[bool] = []
        for n in roots:
            keep, has_score_subtree = prune(n)
            if keep:
                kept_roots.append(n)
                roots_score_flags.append(has_score_subtree)

        for n, has_score in zip(kept_roots, roots_score_flags):
            if not has_score:
                n.num = ""
                setattr(n, "_is_context", True)

        return kept_roots

    def _split_by_score_keep_marker(self, text: str):
        m = self.SCORE_RE.search(text or "")
        if not m:
            return text, False, ""
        before = text[:m.end()].rstrip()
        after = (text[m.end():] or "").lstrip()
        return before, True, after
