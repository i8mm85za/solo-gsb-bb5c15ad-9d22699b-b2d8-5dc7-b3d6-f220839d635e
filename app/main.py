"""FastAPI 入口：文档上传、对齐、审阅决定、手动调整、导出。

启动时初始化 SQLite；静态前端挂载在 /，页面访问 8000 端口。
"""
from __future__ import annotations

import csv
import io
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from . import database as dbmod
from .aligner import align, manual_record, split_paragraphs
from .database import db, indices_to_str, row_to_dict
from .schemas import (
    AlignmentCreate,
    AlignmentPatch,
    AlignmentRemap,
    DocumentCreate,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 容器启动时初始化 SQLite（entrypoint 也会显式执行一次，均为幂等）
    dbmod.init_db()
    yield


app = FastAPI(title="法规条款变更审阅", version="1.0.0", lifespan=lifespan)

CHANGE_TYPES = {
    "unchanged": "未变更",
    "rewrite": "改写",
    "added": "新增",
    "deleted": "删除",
    "split": "拆分",
    "merged": "合并",
    "multi_rewrite": "多段改写",
}
DECISIONS = {
    "pending": "待审阅",
    "confirmed": "确认",
    "disputed": "有异议",
    "ignored": "忽略",
}


@app.on_event("startup")
def _startup() -> None:
    dbmod.init_db()


# ---------------- 文档 ----------------

@app.post("/api/documents", status_code=201)
def create_document(payload: DocumentCreate):
    if not payload.old_text.strip() and not payload.new_text.strip():
        raise HTTPException(422, "旧版与新版文本不能同时为空")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO documents (title, old_text, new_text) VALUES (?, ?, ?)",
            (payload.title.strip(), payload.old_text, payload.new_text),
        )
        doc_id = cur.lastrowid
        old_paras = split_paragraphs(payload.old_text)
        new_paras = split_paragraphs(payload.new_text)
        conn.executemany(
            "INSERT INTO paragraphs (document_id, side, idx, content) VALUES (?, 'old', ?, ?)",
            [(doc_id, i, p) for i, p in enumerate(old_paras)],
        )
        conn.executemany(
            "INSERT INTO paragraphs (document_id, side, idx, content) VALUES (?, 'new', ?, ?)",
            [(doc_id, i, p) for i, p in enumerate(new_paras)],
        )
        records = align(payload.old_text, payload.new_text)
        for r in records:
            conn.execute(
                """INSERT INTO alignments
                   (document_id, old_indices, new_indices, change_type, confidence,
                    explanation, similarity, old_group_text, new_group_text, diff_ops, is_manual)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (doc_id, indices_to_str(r["old_indices"]), indices_to_str(r["new_indices"]),
                 r["change_type"], r["confidence"], r["explanation"],
                 r["similarity"], r["old_group_text"], r["new_group_text"],
                 json.dumps(r["diff_ops"], ensure_ascii=False)),
            )
        conn.execute("UPDATE documents SET aligned_at = datetime('now') WHERE id = ?", (doc_id,))
    return {"id": doc_id, "aligned": len(records)}


@app.get("/api/documents")
def list_documents():
    with db() as conn:
        rows = conn.execute(
            """SELECT d.*,
                      (SELECT COUNT(*) FROM paragraphs p WHERE p.document_id=d.id AND p.side='old') AS n_old,
                      (SELECT COUNT(*) FROM paragraphs p WHERE p.document_id=d.id AND p.side='new') AS n_new,
                      (SELECT COUNT(*) FROM alignments a WHERE a.document_id=d.id) AS n_align,
                      (SELECT COUNT(*) FROM alignments a WHERE a.document_id=d.id AND a.decision!='pending') AS n_reviewed
               FROM documents d ORDER BY d.created_at DESC, d.id DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def _get_doc_or_404(conn, doc_id: int):
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "文档不存在")
    return row


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int):
    with db() as conn:
        doc = _get_doc_or_404(conn, doc_id)
        paras = conn.execute(
            "SELECT side, idx, content FROM paragraphs WHERE document_id=? ORDER BY side, idx",
            (doc_id,),
        ).fetchall()
        aligns = conn.execute(
            "SELECT * FROM alignments WHERE document_id=? ORDER BY id", (doc_id,)
        ).fetchall()
    paragraphs = {"old": [], "new": []}
    for p in paras:
        paragraphs[p["side"]].append({"idx": p["idx"], "content": p["content"]})
    result = dict(doc)
    result["paragraphs"] = paragraphs
    result["alignments"] = []
    for r in aligns:
        a = row_to_dict(r)
        a["diff_ops"] = json.loads(r["diff_ops"] or "[]")
        result["alignments"].append(a)
    return result


@app.delete("/api/documents/{doc_id}", status_code=204)
def delete_document(doc_id: int):
    with db() as conn:
        _get_doc_or_404(conn, doc_id)
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    return Response(status_code=204)


# ---------------- 审阅决定与手动调整 ----------------

def _get_alignment_or_404(conn, doc_id: int, align_id: int):
    row = conn.execute(
        "SELECT * FROM alignments WHERE id=? AND document_id=?", (align_id, doc_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "对齐记录不存在")
    return row


@app.patch("/api/documents/{doc_id}/alignments/{align_id}")
def patch_alignment(doc_id: int, align_id: int, payload: AlignmentPatch):
    with db() as conn:
        _get_doc_or_404(conn, doc_id)
        _get_alignment_or_404(conn, doc_id, align_id)
        fields, params = [], []
        if payload.decision is not None:
            fields.append("decision = ?")
            params.append(payload.decision)
            fields.append("decided_at = ?")
            params.append(datetime.utcnow().isoformat(timespec="seconds"))
        if payload.decision_note is not None:
            fields.append("decision_note = ?")
            params.append(payload.decision_note)
        if fields:
            params.extend([align_id, doc_id])
            conn.execute(
                f"UPDATE alignments SET {', '.join(fields)} WHERE id=? AND document_id=?",
                params,
            )
        row = _get_alignment_or_404(conn, doc_id, align_id)
        a = row_to_dict(row)
        a["diff_ops"] = json.loads(row["diff_ops"] or "[]")
    return a


def _load_paragraphs(conn, doc_id: int) -> tuple[list[str], list[str]]:
    old = [r[0] for r in conn.execute(
        "SELECT content FROM paragraphs WHERE document_id=? AND side='old' ORDER BY idx",
        (doc_id,)).fetchall()]
    new = [r[0] for r in conn.execute(
        "SELECT content FROM paragraphs WHERE document_id=? AND side='new' ORDER BY idx",
        (doc_id,)).fetchall()]
    return old, new


def _validate_indices(indices: list[int], n: int, side: str) -> None:
    if len(set(indices)) != len(indices):
        raise HTTPException(422, f"{side}段落序号存在重复")
    if indices and (min(indices) < 0 or max(indices) >= n):
        raise HTTPException(422, f"{side}段落序号超出范围（共 {n} 段）")


def _apply_manual_record(conn, doc_id: int, old_idx: list[int], new_idx: list[int],
                         forced_type: str | None, note: str = "") -> int:
    """计算手动映射内容并写入，返回新 alignment id。"""
    old, new = _load_paragraphs(conn, doc_id)
    _validate_indices(old_idx, len(old), "旧版")
    _validate_indices(new_idx, len(new), "新版")
    r = manual_record(old_idx, new_idx, old, new)
    if forced_type is not None:
        r["change_type"] = forced_type
        r["explanation"] = "审阅者强制指定变更类型为「%s」。%s" % (
            CHANGE_TYPES[forced_type], r["explanation"])
        r["is_manual"] = True
    cur = conn.execute(
        """INSERT INTO alignments
           (document_id, old_indices, new_indices, change_type, confidence, explanation,
            similarity, old_group_text, new_group_text, diff_ops, is_manual,
            decision, decision_note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'confirmed', ?)""",
        (doc_id, indices_to_str(old_idx), indices_to_str(new_idx),
         r["change_type"], r["confidence"], r["explanation"],
         r["similarity"], r["old_group_text"], r["new_group_text"],
         json.dumps(r["diff_ops"], ensure_ascii=False), note),
    )
    return cur.lastrowid


@app.put("/api/documents/{doc_id}/alignments/{align_id}")
def remap_alignment(doc_id: int, align_id: int, payload: AlignmentRemap):
    """手动调整映射：以调整后的段落组合新建一条人工记录并删除原自动记录。

    这样保留审阅痕迹的同时，段落不会同时出现在两条映射中。
    """
    with db() as conn:
        _get_doc_or_404(conn, doc_id)
        row = _get_alignment_or_404(conn, doc_id, align_id)
        old_idx = row_to_dict(row)["old_indices"] if payload.old_indices is None else payload.old_indices
        new_idx = row_to_dict(row)["new_indices"] if payload.new_indices is None else payload.new_indices
        note = payload.decision_note if payload.decision_note is not None else row["decision_note"]
        new_id = _apply_manual_record(conn, doc_id, old_idx, new_idx,
                                      payload.change_type, note)
        conn.execute("DELETE FROM alignments WHERE id=?", (align_id,))
        row2 = conn.execute("SELECT * FROM alignments WHERE id=?", (new_id,)).fetchone()
        a = row_to_dict(row2)
        a["diff_ops"] = json.loads(row2["diff_ops"] or "[]")
    return a


@app.post("/api/documents/{doc_id}/alignments", status_code=201)
def create_alignment(doc_id: int, payload: AlignmentCreate):
    with db() as conn:
        _get_doc_or_404(conn, doc_id)
        old, new = _load_paragraphs(conn, doc_id)
        _validate_indices(payload.old_indices, len(old), "旧版")
        _validate_indices(payload.new_indices, len(new), "新版")
        if not payload.old_indices and not payload.new_indices:
            raise HTTPException(422, "旧版与新版序号不能同时为空")
        # 同一段落不允许出现在两条映射中，避免一对多歧义
        conflict = conn.execute(
            "SELECT id, old_indices, new_indices FROM alignments WHERE document_id=?",
            (doc_id,),
        ).fetchall()
        o_set, n_set = set(payload.old_indices), set(payload.new_indices)
        for c in conflict:
            co = set(dbmod.str_to_indices(c["old_indices"]))
            cn = set(dbmod.str_to_indices(c["new_indices"]))
            if o_set & co or n_set & cn:
                raise HTTPException(409, f"段落已存在于映射 #{c['id']} 中，请先调整或删除原映射")
        r = manual_record(payload.old_indices, payload.new_indices, old, new)
        ctype = payload.change_type or r["change_type"]
        cur = conn.execute(
            """INSERT INTO alignments
               (document_id, old_indices, new_indices, change_type, confidence, explanation,
                similarity, old_group_text, new_group_text, diff_ops, is_manual,
                decision, decision_note, decided_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, datetime('now'))""",
            (doc_id, indices_to_str(payload.old_indices),
             indices_to_str(payload.new_indices), ctype, r["confidence"],
             r["explanation"] if payload.change_type is None else
             "审阅者强制指定变更类型为「%s」。%s" % (CHANGE_TYPES[ctype], r["explanation"]),
             r["similarity"], r["old_group_text"], r["new_group_text"],
             json.dumps(r["diff_ops"], ensure_ascii=False),
             payload.decision, payload.decision_note),
        )
        row = conn.execute("SELECT * FROM alignments WHERE id=?", (cur.lastrowid,)).fetchone()
        a = row_to_dict(row)
        a["diff_ops"] = json.loads(row["diff_ops"] or "[]")
    return a


@app.delete("/api/documents/{doc_id}/alignments/{align_id}", status_code=204)
def delete_alignment(doc_id: int, align_id: int):
    with db() as conn:
        _get_doc_or_404(conn, doc_id)
        _get_alignment_or_404(conn, doc_id, align_id)
        conn.execute("DELETE FROM alignments WHERE id=?", (align_id,))
    return Response(status_code=204)


@app.post("/api/documents/{doc_id}/realign", status_code=201)
def realign(doc_id: int):
    """丢弃全部自动对齐结果（手动映射也一并清除），按当前文本重新生成。"""
    with db() as conn:
        doc = _get_doc_or_404(conn, doc_id)
        conn.execute("DELETE FROM alignments WHERE document_id=?", (doc_id,))
        records = align(doc["old_text"], doc["new_text"])
        for r in records:
            conn.execute(
                """INSERT INTO alignments
                   (document_id, old_indices, new_indices, change_type, confidence,
                    explanation, similarity, old_group_text, new_group_text, diff_ops)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (doc_id, indices_to_str(r["old_indices"]), indices_to_str(r["new_indices"]),
                 r["change_type"], r["confidence"], r["explanation"],
                 r["similarity"], r["old_group_text"], r["new_group_text"],
                 json.dumps(r["diff_ops"], ensure_ascii=False)),
            )
        conn.execute("UPDATE documents SET aligned_at=datetime('now') WHERE id=?", (doc_id,))
    return {"detail": "已重新对齐", "count": len(records)}


# ---------------- 导出 ----------------

def _filtered_alignments(conn, doc_id: int, change_type: str | None,
                         decision: str | None) -> list:
    sql = "SELECT * FROM alignments WHERE document_id=?"
    params: list = [doc_id]
    if change_type:
        sql += " AND change_type=?"
        params.append(change_type)
    if decision:
        sql += " AND decision=?"
        params.append(decision)
    sql += " ORDER BY id"
    return [row_to_dict(r) | {"diff_ops": json.loads(r["diff_ops"] or "[]")}
            for r in conn.execute(sql, params).fetchall()]


@app.get("/api/documents/{doc_id}/export")
def export_document(
    doc_id: int,
    format: str = Query("csv", pattern="^(csv|json|md)$"),
    change_type: str | None = None,
    decision: str | None = None,
):
    """导出审阅结果。CSV/JSON 为结构化数据；Markdown 为人类可读报告。

    每条记录均含：两版原文（old_group_text/new_group_text）、变更类型、审阅决定。
    """
    with db() as conn:
        doc = _get_doc_or_404(conn, doc_id)
        aligns = _filtered_alignments(conn, doc_id, change_type, decision)
    title = doc["title"]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if format == "json":
        payload = {
            "document": {"id": doc["id"], "title": title,
                         "exported_at": stamp,
                         "old_text": doc["old_text"], "new_text": doc["new_text"]},
            "filter": {"change_type": change_type, "decision": decision},
            "alignments": aligns,
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return Response(content=data, media_type="application/json; charset=utf-8",
                        headers={"Content-Disposition":
                                 f"attachment; filename=regreview-{doc_id}-{stamp}.json"})

    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["序号", "旧版段落号", "新版段落号", "变更类型", "置信度",
                         "相似度", "旧版原文", "新版原文", "判定依据",
                         "是否人工调整", "审阅决定", "审阅备注", "审阅时间"])
        for i, a in enumerate(aligns, 1):
            writer.writerow([
                i,
                "、".join(str(x + 1) for x in a["old_indices"]),
                "、".join(str(x + 1) for x in a["new_indices"]),
                CHANGE_TYPES[a["change_type"]],
                f'{a["confidence"]:.2f}',
                "" if a["similarity"] is None else f'{a["similarity"]:.2f}',
                a["old_group_text"], a["new_group_text"], a["explanation"],
                "是" if a["is_manual"] else "否",
                DECISIONS[a["decision"]], a["decision_note"],
                a["decided_at"] or "",
            ])
        # 加 UTF-8 BOM，便于 Excel 直接打开中文
        data = b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")
        return Response(content=data, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 f"attachment; filename=regreview-{doc_id}-{stamp}.csv"})

    # Markdown 报告
    lines = [f"# 《{title}》条款变更审阅报告", "",
             f"- 导出时间：{stamp}",
             f"- 记录数：{len(aligns)}"
             + (f"（仅筛选变更类型：{CHANGE_TYPES.get(change_type, change_type)}）" if change_type else "")
             + (f"（仅筛选决定：{DECISIONS.get(decision, decision)}）" if decision else ""),
             "", "| # | 旧版段 | 新版段 | 变更类型 | 置信度 | 决定 | 备注 |",
             "|---|---|---|---|---|---|---|"]
    for i, a in enumerate(aligns, 1):
        note = a["decision_note"].replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {i} | {'、'.join(str(x+1) for x in a['old_indices']) or '—'} "
            f"| {'、'.join(str(x+1) for x in a['new_indices']) or '—'} "
            f"| {CHANGE_TYPES[a['change_type']]} | {a['confidence']:.2f} "
            f"| {DECISIONS[a['decision']]} | {note or '—'} |"
        )
    lines.append("")
    for i, a in enumerate(aligns, 1):
        lines += [
            f"## {i}. {CHANGE_TYPES[a['change_type']]}"
            f"（置信度 {a['confidence']:.2f}，决定：{DECISIONS[a['decision']]}）",
            "",
            f"**判定依据**：{a['explanation']}"
            + ("（人工调整）" if a["is_manual"] else ""),
            "",
            "> **旧版原文**（第 %s 段）：" % ("、".join(str(x+1) for x in a["old_indices"]) or "无"),
            "",
        ]
        for ln in (a["old_group_text"] or "（无）").splitlines() or ["（无）"]:
            lines.append(f"> {ln}")
        lines += ["", "> **新版原文**（第 %s 段）：" % ("、".join(str(x+1) for x in a["new_indices"]) or "无"), ""]
        for ln in (a["new_group_text"] or "（无）").splitlines() or ["（无）"]:
            lines.append(f"> {ln}")
        if a["decision_note"]:
            lines += ["", f"**审阅备注**：{a['decision_note']}"]
        lines.append("")
    data = "\n".join(lines).encode("utf-8")
    return Response(content=data, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename=regreview-{doc_id}-{stamp}.md"})


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------------- 前端静态资源 ----------------

STATIC_DIR = Path("/app/static") if os.path.isdir("/app/static") \
    else Path(__file__).resolve().parent.parent / "static"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
