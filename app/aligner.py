"""条款对齐核心算法。

流程（可解释、可复现）：
1. ``split_paragraphs`` 段落切分：优先空行分块，再按条款号（第X条/1./（一）…）细分；
2. ``align`` 第一遍：带权最长公共子序列（weighted LCS）找出高相似度锚点
   （相似度 >= 0.88，顺序保持的最优匹配）；
3. 锚点之间的未匹配区间交给 ``_align_block``：
   Needleman-Wunsch 风格动态规划得到 0/1 配对，再后处理识别
   合并（多旧→一新）、拆分（一旧→多新），其余按新增/删除处理；
4. 每条候选输出：变更类型、置信度、相似度、中文解释、字符级 diff。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# ---------- 常量阈值 ----------
ANCHOR_SIM = 0.88        # 锚点（确定的未改写/轻微改写匹配）阈值
REWRITE_SIM = 0.30       # 1:1 判定为改写的最低相似度
GROUP_SIM = 0.45         # 合并/拆分组与单段判定阈值
SPLIT_CLAUSE = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百零〇0-9]+[条章节款项]"
    r"|[0-9]{1,2}[\.、)]"
    r"|[（(][一二三四五六七八九十百零〇0-9]+[）)])\s*"
)


def split_paragraphs(text: str) -> list[str]:
    """纯文本 → 段落列表。

    先按一个以上空行分块；块内若出现多个条款号开头的行（如逐行排列的
    "第一条 … 第二条 …"），再按条款号边界细分。最后回退到按非空行切分。
    保留原文，仅去除行尾空白；不做标点归一化（归一化只用于相似度计算）。
    """
    if not text or not text.strip():
        return []

    blocks = re.split(r"\n\s*\n", text.strip())
    out: list[str] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        starts = [i for i, ln in enumerate(lines) if SPLIT_CLAUSE.match(ln)]
        if len(starts) >= 2:
            bounds = starts + [len(lines)]
            for a, b in zip(bounds, bounds[1:]):
                seg = "\n".join(lines[a:b]).strip()
                if seg:
                    out.append(seg)
        else:
            out.append("\n".join(lines))

    # 没有空行且没有条款号结构时，按非空行切分
    if len(out) <= 1:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) > 1:
            return lines
    return out


_NORMALIZE_PAT = re.compile(r"[\s　，。；：、“”‘’（）()【】《》,.;:\"'?!？！\-—_0-9０-９]")


def _normalize(s: str) -> str:
    return _NORMALIZE_PAT.sub("", s)


def similarity(a: str, b: str, _cache: dict | None = None) -> float:
    """混合相似度 = 0.4*原文 difflib 比率 + 0.6*去标点空白后比率。

    原文比率保留结构信息，归一化比率避免标点/空白差异拉低中文条款相似度。
    """
    key = (a, b)
    if _cache is not None and key in _cache:
        return _cache[key]
    if not a or not b:
        val = 0.0
    else:
        raw = SequenceMatcher(None, a, b, autojunk=False).ratio()
        na, nb = _normalize(a), _normalize(b)
        norm = SequenceMatcher(None, na, nb, autojunk=False).ratio() if na and nb else 0.0
        val = round(0.4 * raw + 0.6 * norm, 6)
    if _cache is not None:
        _cache[key] = val
    return val


def char_diff(a: str, b: str) -> list[list]:
    """SequenceMatcher opcodes → [tag, i1, i2, j1, j2] 列表，前端做字符级高亮。"""
    return [list(op) for op in SequenceMatcher(None, a, b, autojunk=False).get_opcodes()]


def _confidence_from_sim(sim: float, change: str) -> float:
    """由相似度与变更类型给出置信度（0~1）。"""
    if change == "unchanged":
        return round(0.9 + 0.1 * sim, 4)          # 0.99~1.0
    if change == "rewrite":
        # 0.3→约0.5，0.88→约0.9，区间映射到 0.5~0.92
        t = (sim - REWRITE_SIM) / (ANCHOR_SIM - REWRITE_SIM)
        return round(min(0.92, 0.50 + 0.40 * t), 4)
    if change in ("split", "merged"):
        return round(min(0.9, 0.55 + 0.35 * sim), 4)
    return round(0.75, 4)                          # added / deleted / multi_rewrite


@dataclass
class Rec:
    old_indices: list[int] = field(default_factory=list)
    new_indices: list[int] = field(default_factory=list)
    change_type: str = "rewrite"
    confidence: float = 0.0
    explanation: str = ""
    similarity: float | None = None
    old_group_text: str = ""
    new_group_text: str = ""
    diff_ops: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "old_indices": self.old_indices,
            "new_indices": self.new_indices,
            "change_type": self.change_type,
            "confidence": round(self.confidence, 4),
            "explanation": self.explanation,
            "similarity": round(self.similarity, 4) if self.similarity is not None else None,
            "old_group_text": self.old_group_text,
            "new_group_text": self.new_group_text,
            "diff_ops": self.diff_ops,
            "is_manual": False,
        }


def _pair_rec(oi: int, ni: int, old: list[str], new: list[str], cache: dict,
              force_type: str | None = None) -> Rec:
    """1:1 配对记录：阈值判定 unchanged / rewrite；过低相似度时 force_type 决定删除+新增还是多改。"""
    o, n = old[oi], new[ni]
    sim = similarity(o, n, cache)
    if force_type == "multi_rewrite":
        ctype = "multi_rewrite"
        reason = "段落内容大幅改写，仅保留少量对应成分"
    elif sim >= ANCHOR_SIM:
        ctype = "unchanged"
        reason = "两版文本高度一致（相似度 %.0f%%），视为未变更" % (sim * 100)
    elif sim >= REWRITE_SIM:
        ctype = "rewrite"
        reason = "两版存在明确对应关系但有文字改动（相似度 %.0f%%），判定为改写" % (sim * 100)
    elif force_type == "deleted_added":
        ctype = "rewrite"
        reason = "对齐到同一段落但文字差异很大（相似度 %.0f%%），按旧段删除＋新段新增理解" % (sim * 100)
    else:
        ctype = "rewrite"
        reason = "两版存在对应关系但文字差异较大（相似度 %.0f%%），判定为改写" % (sim * 100)
    return Rec(
        old_indices=[oi], new_indices=[ni], change_type=ctype,
        confidence=_confidence_from_sim(sim, ctype), explanation=reason,
        similarity=sim, old_group_text=o, new_group_text=n,
        diff_ops=char_diff(o, n),
    )


def _added_rec(ni: int, new: list[str], cache: dict, old: list[str]) -> Rec:
    n = new[ni]
    best = max((similarity(n, o, cache) for o in old), default=0.0)
    if best >= REWRITE_SIM:
        reason = "新版第 %d 段在旧版中无顺序对应段落；与最相似旧段相似度 %.0f%%，仍判定为新增" % (
            ni + 1, best * 100)
    else:
        reason = "新版第 %d 段在旧版中没有对应段落，判定为新增" % (ni + 1)
    return Rec(
        new_indices=[ni], change_type="added", confidence=0.75,
        explanation=reason, similarity=best if old else None,
        new_group_text=n, diff_ops=char_diff("", n),
    )


def _deleted_rec(oi: int, old: list[str], cache: dict, new: list[str]) -> Rec:
    o = old[oi]
    best = max((similarity(o, n, cache) for n in new), default=0.0)
    if best >= REWRITE_SIM:
        reason = "旧版第 %d 段在新版中无顺序对应段落；与最相似新段相似度 %.0f%%，仍判定为删除" % (
            oi + 1, best * 100)
    else:
        reason = "旧版第 %d 段在新版中没有对应段落，判定为删除" % (oi + 1)
    return Rec(
        old_indices=[oi], change_type="deleted", confidence=0.75,
        explanation=reason, similarity=best if new else None,
        old_group_text=o, diff_ops=char_diff(o, ""),
    )


def _merge_rec(ois: list[int], ni: int, old: list[str], new: list[str], cache: dict) -> Rec:
    """多旧 → 一新：合并。"""
    og = "\n".join(old[i] for i in ois)
    n = new[ni]
    sim = similarity(og, n, cache)
    best_single = max(similarity(old[i], n, cache) for i in ois)
    if sim >= best_single + 0.02:
        reason = "旧版第 %s 段合并后与新版第 %d 段对应（整组相似度 %.0f%%，高于任一单段 %.0f%%），判定为合并" % (
            _label(ois), ni + 1, sim * 100, best_single * 100)
    else:
        reason = "旧版第 %s 段共同对应新版第 %d 段（整组相似度 %.0f%%），判定为合并" % (
            _label(ois), ni + 1, sim * 100)
    return Rec(
        old_indices=list(ois), new_indices=[ni], change_type="merged",
        confidence=_confidence_from_sim(sim, "merged"), explanation=reason,
        similarity=sim, old_group_text=og, new_group_text=n,
        diff_ops=char_diff(og, n),
    )


def _split_rec(oi: int, nis: list[int], old: list[str], new: list[str], cache: dict) -> Rec:
    """一旧 → 多新：拆分。"""
    o = old[oi]
    ng = "\n".join(new[i] for i in nis)
    sim = similarity(o, ng, cache)
    best_single = max(similarity(o, new[i], cache) for i in nis)
    if sim >= best_single + 0.02:
        reason = "旧版第 %d 段拆分后对应新版第 %s 段（整组相似度 %.0f%%，高于任一单段 %.0f%%），判定为拆分" % (
            oi + 1, _label(nis), sim * 100, best_single * 100)
    else:
        reason = "旧版第 %d 段对应新版第 %s 段（整组相似度 %.0f%%），判定为拆分" % (
            oi + 1, _label(nis), sim * 100)
    return Rec(
        old_indices=[oi], new_indices=list(nis), change_type="split",
        confidence=_confidence_from_sim(sim, "split"), explanation=reason,
        similarity=sim, old_group_text=o, new_group_text=ng,
        diff_ops=char_diff(o, ng),
    )


def _label(indices: list[int]) -> str:
    """[0,2,3] -> '1、3、4'（面向用户的 1 基段落号）。"""
    return "、".join(str(i + 1) for i in indices)


# ---------- 第一遍：weighted-LCS 锚点 ----------

def _find_anchors(old: list[str], new: list[str], cache: dict) -> list[tuple[int, int, float]]:
    """带权 LCS：在所有 >= ANCHOR_SIM 的候选对中求总相似度最大的保序匹配。"""
    n, m = len(old), len(new)
    # 候选集合（用字符集合 Jaccard 做快速预筛）
    cand: dict[int, list[tuple[int, float]]] = {}
    new_sets = [set(_normalize(x)) for x in new]
    for i in range(n):
        oi = old[i]
        os_ = set(_normalize(oi))
        row: list[tuple[int, float]] = []
        for j in range(m):
            nj = new[j]
            union = len(os_ | new_sets[j])
            jac = len(os_ & new_sets[j]) / union if union else 0.0
            if jac < 0.3:
                continue
            sim = similarity(oi, nj, cache)
            if sim >= ANCHOR_SIM:
                row.append((j, sim))
        if row:
            cand[i] = row

    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    take = [[False] * (m + 1) for _ in range(n + 1)]
    sim_at = [[-1.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n):
        for j, s in cand.get(i, []):
            sim_at[i + 1][j + 1] = s
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best = dp[i - 1][j]
            best_take = False
            if dp[i][j - 1] > best:
                best = dp[i][j - 1]
                best_take = False
            s = sim_at[i][j]
            if s >= 0.0:
                v = dp[i - 1][j - 1] + s
                if v > best:
                    best = v
                    best_take = True
            dp[i][j] = best
            take[i][j] = best_take

    anchors: list[tuple[int, int, float]] = []
    i, j = n, m
    while i > 0 and j > 0:
        if take[i][j]:
            anchors.append((i - 1, j - 1, sim_at[i][j]))
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1
    anchors.reverse()
    return anchors


# ---------- 区间对齐：NW + 合并/拆分后处理 ----------

def _needleman_wunsch(old_b: list[str], new_b: list[str], cache: dict) -> list[str]:
    """局部区间的序列对齐，返回逐列操作：'=' / 'd' / 'a'。"""
    n, m = len(old_b), len(new_b)
    GAP = -0.6
    match = [[0.0] * m for _ in range(n)]
    for i in range(n):
        for j in range(m):
            s = similarity(old_b[i], new_b[j], cache)
            # 转成 -1~1 的配对收益
            match[i][j] = (s - 0.5) * 2.0
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    back = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = i * GAP
        back[i][0] = "up"
    for j in range(1, m + 1):
        dp[0][j] = j * GAP
        back[0][j] = "left"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            diag = dp[i - 1][j - 1] + match[i - 1][j - 1]
            up = dp[i - 1][j] + GAP
            left = dp[i][j - 1] + GAP
            if diag >= up and diag >= left:
                dp[i][j] = diag
                back[i][j] = "diag"
            elif up >= left:
                dp[i][j] = up
                back[i][j] = "up"
            else:
                dp[i][j] = left
                back[i][j] = "left"
    ops: list[str] = []
    i, j = n, m
    while i > 0 or j > 0:
        b = back[i][j]
        if b == "diag":
            ops.append("=")
            i -= 1
            j -= 1
        elif b == "up":
            ops.append("d")
            i -= 1
        else:
            ops.append("a")
            j -= 1
    ops.reverse()
    return ops


def _align_block(o_start: int, o_end: int, n_start: int, n_end: int,
                 old: list[str], new: list[str], cache: dict) -> list[Rec]:
    """对齐两个锚点之间（或边界处）的未匹配区间，全局索引 [start, end)。"""
    ois = list(range(o_start, o_end))
    nis = list(range(n_start, n_end))
    if not ois and not nis:
        return []
    if not ois:
        return [_added_rec(j, new, cache, old) for j in nis]
    if not nis:
        return [_deleted_rec(i, old, cache, new) for i in ois]

    # 1 旧 1 新：直接配对
    if len(ois) == 1 and len(nis) == 1:
        return [_pair_rec(ois[0], nis[0], old, new, cache)]

    old_b = [old[i] for i in ois]
    new_b = [new[j] for j in nis]
    ops = _needleman_wunsch(old_b, new_b, cache)

    # 把操作流按列分组：连续 d 游程 / a 游程 / 单个配对
    groups: list[dict] = []
    col_i = col_j = 0
    for op in ops:
        if op == "=":
            groups.append({"tag": "=", "li": col_i, "lj": col_j})
            col_i += 1
            col_j += 1
        elif op == "d":
            if groups and groups[-1]["tag"] == "d":
                groups[-1]["lis"].append(col_i)
            else:
                groups.append({"tag": "d", "lis": [col_i]})
            col_i += 1
        else:  # 'a'
            if groups and groups[-1]["tag"] == "a":
                groups[-1]["ljs"].append(col_j)
            else:
                groups.append({"tag": "a", "ljs": [col_j]})
            col_j += 1

    def adjacent(g: int, tag: str) -> list[int]:
        """返回与第 g 组直接相邻（跳过另一侧游程）的同类型游程组索引。"""
        out = []
        for g2 in range(g - 1, -1, -1):
            t = groups[g2]["tag"]
            if t == tag:
                out.append(g2)
                break
            if t == "=":
                break
        for g2 in range(g + 1, len(groups)):
            t = groups[g2]["tag"]
            if t == tag:
                out.append(g2)
                break
            if t == "=":
                break
        return out

    # ---- 统一决策：每个配对列提出吸收相邻 d（合并）/a（拆分）游程的候选 ----
    # requests[run_group_idx] = [(pair_group_idx, kind, group_sim), ...]
    requests: dict[int, list[tuple[int, str, float]]] = {}
    pair_cand: dict[int, dict] = {}

    for g, grp in enumerate(groups):
        if grp["tag"] != "=":
            continue
        base_oi, base_ni = ois[grp["li"]], nis[grp["lj"]]
        base_sim = similarity(old[base_oi], new[base_ni], cache)

        merge_runs, merge_sim, merge_ois = [], base_sim, [base_oi]
        split_runs, split_sim, split_nis = [], base_sim, [base_ni]
        for rg in adjacent(g, "d"):
            cand = merge_ois + [ois[li] for li in groups[rg]["lis"]]
            s = similarity("\n".join(old[i] for i in cand), new[base_ni], cache)
            if s >= merge_sim + 0.01 and s >= GROUP_SIM:
                merge_ois, merge_sim = cand, s
                merge_runs.append(rg)
        for rg in adjacent(g, "a"):
            cand = split_nis + [nis[lj] for lj in groups[rg]["ljs"]]
            s = similarity(old[base_oi], "\n".join(new[j] for j in cand), cache)
            if s >= split_sim + 0.01 and s >= GROUP_SIM:
                split_nis, split_sim = cand, s
                split_runs.append(rg)

        kind = None
        if len(merge_ois) >= 2 and len(split_nis) >= 2:
            kind = "merged" if merge_sim >= split_sim else "split"
        elif len(merge_ois) >= 2:
            kind = "merged"
        elif len(split_nis) >= 2:
            kind = "split"
        if kind:
            runs = merge_runs if kind == "merged" else split_runs
            sim = merge_sim if kind == "merged" else split_sim
            pair_cand[g] = {"kind": kind, "runs": runs, "sim": sim}
            for rg in runs:
                requests.setdefault(rg, []).append((g, kind, sim))

    # 游程被多个配对列争抢时，整组相似度最高者得
    owner: dict[int, int] = {}
    for rg, bids in requests.items():
        owner[rg] = max(bids, key=lambda b: b[2])[0]

    # ---- 产出记录 ----
    recs: list[Rec] = []
    used_runs: set[int] = set()
    for g, cand in pair_cand.items():
        won = [rg for rg in cand["runs"] if owner.get(rg) == g]
        if not won:
            continue
        grp = groups[g]
        used_runs.update(won)
        if cand["kind"] == "merged":
            m_ois = [ois[grp["li"]]] + [ois[li] for rg in won for li in groups[rg]["lis"]]
            m_ois = sorted(set(m_ois))
            if len(m_ois) >= 2:
                recs.append(_merge_rec(m_ois, nis[grp["lj"]], old, new, cache))
            else:
                recs.append(_pair_rec(m_ois[0], nis[grp["lj"]], old, new, cache))
        else:
            m_nis = [nis[grp["lj"]]] + [nis[lj] for rg in won for lj in groups[rg]["ljs"]]
            m_nis = sorted(set(m_nis))
            if len(m_nis) >= 2:
                recs.append(_split_rec(ois[grp["li"]], m_nis, old, new, cache))
            else:
                recs.append(_pair_rec(ois[grp["li"]], m_nis[0], old, new, cache))
    pair_done = {g for g in pair_cand if any(owner.get(rg) == g for rg in pair_cand[g]["runs"])}

    # 收集未消费的连续游程：纯 d/a 块仍可识别 多旧→一新 / 一旧→多新。
    # 中间隔着配对列即断开为不同簇。
    clusters: list[list[int]] = []
    for g, grp in enumerate(groups):
        if grp["tag"] in ("d", "a") and g not in used_runs:
            if clusters and g == clusters[-1][-1] + 1:
                clusters[-1].append(g)
            else:
                clusters.append([g])
    cluster_used: set[int] = set()
    for cl in clusters:
        d_groups = [g for g in cl if groups[g]["tag"] == "d"]
        a_groups = [g for g in cl if groups[g]["tag"] == "a"]
        d_idxs = [ois[li] for g in d_groups for li in groups[g]["lis"]]
        a_idxs = [nis[lj] for g in a_groups for lj in groups[g]["ljs"]]
        if len(d_idxs) >= 2 and len(a_idxs) == 1:
            og = "\n".join(old[i] for i in d_idxs)
            if similarity(og, new[a_idxs[0]], cache) >= GROUP_SIM:
                recs.append(_merge_rec(d_idxs, a_idxs[0], old, new, cache))
                cluster_used.update(cl)
                continue
        if len(d_idxs) == 1 and len(a_idxs) >= 2:
            ng = "\n".join(new[j] for j in a_idxs)
            if similarity(old[d_idxs[0]], ng, cache) >= GROUP_SIM:
                recs.append(_split_rec(d_idxs[0], a_idxs, old, new, cache))
                cluster_used.update(cl)
                continue
        if len(d_idxs) == 1 and len(a_idxs) == 1:
            recs.append(_pair_rec(d_idxs[0], a_idxs[0], old, new, cache))
            cluster_used.update(cl)
            continue

    for g, grp in enumerate(groups):
        if grp["tag"] == "=":
            if g in pair_done:
                continue
            recs.append(_pair_rec(ois[grp["li"]], nis[grp["lj"]], old, new, cache))
        elif g in used_runs or g in cluster_used:
            continue
        elif grp["tag"] == "d":
            for li in grp["lis"]:
                recs.append(_deleted_rec(ois[li], old, cache, new))
        else:
            for lj in grp["ljs"]:
                recs.append(_added_rec(nis[lj], new, cache, old))

    recs.sort(key=lambda r: min(r.old_indices + r.new_indices))
    return recs


# ---------- 入口 ----------

def align(old_text: str, new_text: str) -> list[dict]:
    """对齐两版全文，返回候选映射记录（按段落顺序排序）。"""
    old = split_paragraphs(old_text)
    new = split_paragraphs(new_text)
    cache: dict = {}

    if not old:
        return [_added_rec(j, new, cache, old).to_dict() for j in range(len(new))]
    if not new:
        return [_deleted_rec(i, old, cache, new).to_dict() for i in range(len(old))]

    anchors = _find_anchors(old, new, cache)

    recs: list[Rec] = []
    prev_o = prev_n = 0
    for oi, ni, sim in anchors:
        recs.extend(_align_block(prev_o, oi, prev_n, ni, old, new, cache))
        recs.append(_pair_rec(oi, ni, old, new, cache))
        prev_o, prev_n = oi + 1, ni + 1
    recs.extend(_align_block(prev_o, len(old), prev_n, len(new), old, new, cache))

    recs.sort(key=lambda r: min(r.old_indices + r.new_indices))
    return [r.to_dict() for r in recs]


def manual_record(old_indices: list[int], new_indices: list[int],
                  old: list[str], new: list[str], cache: dict | None = None) -> dict:
    """审阅者手动建立/修改映射时，按段落组合推断类型、置信度与解释。"""
    cache = cache if cache is not None else {}
    og = "\n".join(old[i] for i in old_indices) if old_indices else ""
    ng = "\n".join(new[i] for i in new_indices) if new_indices else ""
    no, nn = len(old_indices), len(new_indices)

    if no == 0 and nn > 0:
        r = _added_rec(new_indices[0] if nn == 1 else -1, new, cache, old)
        if nn > 1:
            r.new_indices = list(new_indices)
            r.new_group_text = ng
            r.explanation = "审阅者手动标记：新版第 %s 段在旧版中无对应，判定为新增" % _label(new_indices)
        return r.to_dict()
    if nn == 0 and no > 0:
        r = _deleted_rec(old_indices[0] if no == 1 else -1, old, cache, new)
        if no > 1:
            r.old_indices = list(old_indices)
            r.old_group_text = og
            r.explanation = "审阅者手动标记：旧版第 %s 段在新版中无对应，判定为删除" % _label(old_indices)
        return r.to_dict()
    if no == 1 and nn == 1:
        rec = _pair_rec(old_indices[0], new_indices[0], old, new, cache)
        rec.explanation = "审阅者手动确认的 1:1 映射。" + rec.explanation
        d = rec.to_dict()
        d["is_manual"] = True
        return d
    if no >= 2 and nn == 1:
        rec = _merge_rec(old_indices, new_indices[0], old, new, cache)
        rec.explanation = "审阅者手动建立的合并映射。" + rec.explanation
        d = rec.to_dict()
        d["is_manual"] = True
        return d
    if no == 1 and nn >= 2:
        rec = _split_rec(old_indices[0], new_indices, old, new, cache)
        rec.explanation = "审阅者手动建立的拆分映射。" + rec.explanation
        d = rec.to_dict()
        d["is_manual"] = True
        return d
    # 多对多
    sim = similarity(og, ng, cache)
    rec = Rec(
        old_indices=list(old_indices), new_indices=list(new_indices),
        change_type="multi_rewrite", confidence=round(0.7 + 0.2 * sim, 4),
        explanation="审阅者手动建立的多对多映射（旧版第 %s 段 ↔ 新版第 %s 段，整组相似度 %.0f%%），可能包含拆分后再合并" % (
            _label(old_indices), _label(new_indices), sim * 100),
        similarity=sim, old_group_text=og, new_group_text=ng,
        diff_ops=char_diff(og, ng),
    )
    d = rec.to_dict()
    d["is_manual"] = True
    return d
