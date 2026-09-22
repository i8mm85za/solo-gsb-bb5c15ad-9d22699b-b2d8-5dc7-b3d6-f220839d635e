"""请求/响应 Pydantic 模型。段落序号一律使用 0 基索引。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ChangeType = Literal[
    "unchanged", "rewrite", "added", "deleted", "split", "merged", "multi_rewrite"
]
Decision = Literal["pending", "confirmed", "disputed", "ignored"]


class DocumentCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    old_text: str = ""
    new_text: str = ""


class AlignmentPatch(BaseModel):
    """审阅决定 / 备注更新；不改动映射本身。"""
    decision: Decision | None = None
    decision_note: str | None = None


class AlignmentRemap(BaseModel):
    """手动调整某条映射的段落对应关系（传空数组表示该侧无对应）。"""
    old_indices: list[int] | None = None
    new_indices: list[int] | None = None
    change_type: ChangeType | None = None  # 可强制指定类型，否则按内容推断
    decision_note: str | None = None


class AlignmentCreate(BaseModel):
    """在自动结果之外手动新建一条映射。"""
    old_indices: list[int] = []
    new_indices: list[int] = []
    change_type: ChangeType | None = None
    decision: Decision = "confirmed"
    decision_note: str = ""


class AlignmentOut(BaseModel):
    id: int
    old_indices: list[int]
    new_indices: list[int]
    change_type: ChangeType
    confidence: float
    explanation: str
    similarity: float | None
    old_group_text: str
    new_group_text: str
    diff_ops: list
    is_manual: bool
    decision: Decision
    decision_note: str
    decided_at: str | None
