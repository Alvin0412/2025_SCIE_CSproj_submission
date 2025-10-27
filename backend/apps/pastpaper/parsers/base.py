from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

import abc


class BaseParser(abc.ABC):
    def parse_lines(self, text: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def parse_pdf(self, file_path: str) -> List[Dict[str, Any]]:
        raise NotImplementedError


@dataclass
class QNode:
    num: str  # 显示用题号，如 "1"、"(a)"、"(i)"
    level: int  # 0/1/2 -> 数字/字母/罗马
    norm: str  # 规范化题号：'1'/'a'/'i'
    text_lines: List[str] = field(default_factory=list)
    score: Optional[int] = None
    children: List["QNode"] = field(default_factory=list)

    def add_text(self, line: str):
        if line.strip():
            self.text_lines.append(line)

    def to_dict(self) -> Dict[str, Any]:
        d = {"num": self.num}
        if self.text_lines:
            d["content"] = "\n".join(self.text_lines).strip()
        if self.score is not None:
            d["score"] = self.score
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        if getattr(self, "_is_context", False):
            d["type"] = "context"
        return d
