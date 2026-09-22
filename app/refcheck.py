"""新版条款内部引用检查。

扫描新版段落中的引用表达（「第十条」「第十条第二款」「第十条至第十二条」
「本条」「本条前款 / 前款」「上一条」等），按新版段落结构（条 → 款）解析
其指向，产出可解释的检查记录：

- ``ok``        唯一目标定位成功；
- ``missing``   目标不存在（条号缺失 / 款超出范围 / 无前款等）；
- ``ambiguous`` 存在多个候选（如新版中条号重复），不静默选取，全部列出。

纯确定性规则解析，不依赖任何模型；正则与常量集中在顶部，便于调参。
"""
from __future__ import annotations

import re

# ---------- 数字解析（中文 / 阿拉伯 / 全角数字） ----------

_FW2HW = str.maketrans("０１２３４５６７８９", "0123456789")
_CN_DIGIT = {
    "零": 0, "〇": 0,
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def parse_number(s: str | None) -> int | None:
    """中文/阿拉伯数字 → int；无法解析返回 None（支持 0~9999）。"""
    if not s:
        return None
    s = s.strip().translate(_FW2HW)
    if s.isdigit():
        return int(s)
    total, num = 0, 0
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            total += (num or 1) * _CN_UNIT[ch]  # 「十」= 一十
            num = 0
        else:
            return None
    return total + num


# ---------- 引用与结构正则 ----------

_NUM = r"0-9０-９一二三四五六七八九十百千零〇两"

# 交替顺序即优先级：区间 > 绝对（含款/项后缀）> 本条系列 > 前款 > 上/下一条
REF_RE = re.compile(
    rf"第(?P<range_from>[{_NUM}]+)条至第?(?P<range_to>[{_NUM}]+)条"
    rf"|第(?P<tiao>[{_NUM}]+)条(?:第(?P<kuan>[{_NUM}]+)款)?(?:第(?P<xiang>[{_NUM}]+)项)?"
    rf"|本条第(?P<self_kuan>[{_NUM}]+)款"
    rf"|本条前(?P<self_prev>[{_NUM}]*)款"
    rf"|本条"
    rf"|前(?P<prev_kuan>[{_NUM}]*)款"
    rf"|(?P<rel_article>上一条|下一条|前一条|后一条)"
)

ARTICLE_HEAD_RE = re.compile(rf"^\s*第([{_NUM}]+)条")
ITEM_MARK_RE = re.compile(rf"[（(]([{_NUM}]+)[）)]")


def build_structure(paras: list[str]) -> tuple[list[dict], dict[int, int]]:
    """新版段落 → 条文结构。

    返回 ``(articles, para_article)``：

    - ``articles``：``[{"num": 条号或 None, "paras": [段落 idx, ...]}]``，
      以「第X条」开头的段落开启新条文，其后每个段落依次为该条的款；
    - ``para_article``：段落 idx → 所属条文在 ``articles`` 中的位置
      （首个条文之前的前言段落不在其中）。
    """
    articles: list[dict] = []
    for idx, content in enumerate(paras):
        m = ARTICLE_HEAD_RE.match(content)
        if m:
            articles.append({"num": parse_number(m.group(1)), "paras": [idx]})
        elif articles:
            articles[-1]["paras"].append(idx)
    para_article = {i: pos for pos, art in enumerate(articles) for i in art["paras"]}
    return articles, para_article


# ---------- 定位结果构造 ----------

def _ok(kind: str, targets: list[int], detail: str) -> dict:
    return {"kind": kind, "status": "ok", "targets": targets,
            "candidates": [], "detail": detail}


def _missing(kind: str, detail: str) -> dict:
    return {"kind": kind, "status": "missing", "targets": [],
            "candidates": [], "detail": detail}


def _ambiguous(kind: str, candidates: list[int], detail: str) -> dict:
    return {"kind": kind, "status": "ambiguous", "targets": [],
            "candidates": candidates, "detail": detail}


def _find_articles(articles: list[dict], num: int) -> list[dict]:
    return [a for a in articles if a["num"] == num]


def _article_label(art: dict) -> str:
    return f"第{art['num']}条" if art["num"] is not None else "未编号条文"


def _seg(indices: list[int]) -> str:
    """[0, 2] -> '1、3'（面向用户的 1 基段落号）。"""
    return "、".join(str(i + 1) for i in indices)


# ---------- 各类引用的定位 ----------

def _resolve_absolute(tiao: str, kuan: str | None, xiang: str | None,
                      articles: list[dict], paras: list[str]) -> dict:
    """第N条 [第M款 [第K项]] 绝对引用定位。"""
    num = parse_number(tiao)
    if num is None:
        return _missing("absolute", f"无法解析条号「{tiao}」")
    label = f"第{tiao}条"
    cands = _find_articles(articles, num)
    if not cands:
        return _missing("absolute", f"新版文本中未找到{label}，引用目标缺失")
    if len(cands) > 1:
        where = "、".join(f"第 {a['paras'][0] + 1} 段" for a in cands)
        return _ambiguous(
            "absolute", [a["paras"][0] for a in cands],
            f"新版文本中存在 {len(cands)} 个「{label}」（分别位于{where}），"
            "无法唯一确定目标，需人工确认")
    art = cands[0]
    if kuan is None:
        return _ok("absolute", art["paras"],
                   f"定位到{label}（第 {art['paras'][0] + 1} 段起，共 {len(art['paras'])} 款）")
    k = parse_number(kuan)
    if k is None:
        return _missing("absolute", f"无法解析款号「{kuan}」")
    if not 1 <= k <= len(art["paras"]):
        return _missing("absolute",
                        f"{label}共 {len(art['paras'])} 款，未找到第{kuan}款，引用目标缺失")
    target = art["paras"][k - 1]
    if xiang is not None:
        x = parse_number(xiang)
        marks = {parse_number(g) for g in ITEM_MARK_RE.findall(paras[target])}
        if x not in marks:
            return _missing("absolute",
                            f"{label}第{kuan}款中未找到第{xiang}项（该款下无对应编号），引用目标缺失")
        return _ok("absolute", [target],
                   f"定位到{label}第{kuan}款第{xiang}项（新版第 {target + 1} 段）")
    return _ok("absolute", [target],
               f"定位到{label}第{kuan}款（新版第 {target + 1} 段）")


def _resolve_range(a: str, b: str, articles: list[dict]) -> dict:
    """第N条至第M条 区间引用定位：区间内每一条都必须唯一存在。"""
    na, nb = parse_number(a), parse_number(b)
    if na is None or nb is None:
        return _missing("absolute", f"无法解析引用区间「第{a}条至第{b}条」")
    if na > nb:
        return _missing("absolute", f"引用区间「第{a}条至第{b}条」起点大于终点，无法定位")
    targets: list[int] = []
    for n in range(na, nb + 1):
        cands = _find_articles(articles, n)
        if not cands:
            return _missing("absolute",
                            f"引用区间（第{a}条至第{b}条）内未找到第{n}条，引用目标缺失")
        if len(cands) > 1:
            where = "、".join(f"第 {x['paras'][0] + 1} 段" for x in cands)
            return _ambiguous(
                "absolute", [x["paras"][0] for x in cands],
                f"引用区间内第{n}条存在 {len(cands)} 个候选（{where}），无法唯一确定目标")
        targets.extend(cands[0]["paras"])
    return _ok("absolute", targets,
               f"定位到第{a}条至第{b}条（新版第 {_seg(targets)} 段）")


def _resolve_relative(ref_text: str, m: re.Match, idx: int,
                      articles: list[dict], para_article: dict[int, int]) -> dict:
    """本条 / 本条第X款 / （本条）前X款 / 上一条 / 下一条 相对指代定位。"""
    pos = para_article.get(idx)
    if pos is None:
        return _missing("relative",
                        "该段落位于首个条文之前（前言部分），无所属条文，无法解析相对引用")
    art = articles[pos]
    paras_of = art["paras"]
    kuan_pos = paras_of.index(idx) + 1  # 来源段落在条内的款序号（1 基）
    label = _article_label(art)

    if ref_text == "本条":
        return _ok("relative", paras_of,
                   f"指本条文（{label}，第 {paras_of[0] + 1} 段起，共 {len(paras_of)} 款）")

    if ref_text.startswith("本条第"):
        k = parse_number(m.group("self_kuan"))
        if k is None or not 1 <= k <= len(paras_of):
            return _missing("relative",
                            f"{label}共 {len(paras_of)} 款，未找到「{ref_text}」所指之款")
        t = paras_of[k - 1]
        return _ok("relative", [t], f"定位到{label}第{k}款（新版第 {t + 1} 段）")

    if ref_text.startswith("本条前") or ref_text.startswith("前"):
        n = parse_number(m.group("self_prev") or m.group("prev_kuan")) or 1
        if kuan_pos - n < 1:
            return _missing("relative",
                            f"本款为{label}第{kuan_pos}款，其前不足 {n} 款，「{ref_text}」无目标")
        targets = paras_of[kuan_pos - n - 1:kuan_pos - 1]
        return _ok("relative", targets,
                   f"定位到{label}第{kuan_pos}款之前 {n} 款（新版第 {_seg(targets)} 段）")

    # 上一条 / 前一条 / 下一条 / 后一条
    if ref_text in ("上一条", "前一条"):
        if pos == 0:
            return _missing("relative", f"{label}之前没有其他条文，「{ref_text}」无目标")
        t = articles[pos - 1]
        return _ok("relative", t["paras"],
                   f"定位到上一条文（{_article_label(t)}，第 {t['paras'][0] + 1} 段起）")
    if pos == len(articles) - 1:
        return _missing("relative", f"{label}之后没有其他条文，「{ref_text}」无目标")
    t = articles[pos + 1]
    return _ok("relative", t["paras"],
               f"定位到下一条文（{_article_label(t)}，第 {t['paras'][0] + 1} 段起）")


# ---------- 入口 ----------

def check_references(paras: list[str]) -> list[dict]:
    """扫描新版段落列表，返回引用检查记录（按来源段落与出现位置排序）。

    每条记录：``source_idx``（0 基来源段落）、``ref_text``、``position``
    （在来源段落中的字符偏移）、``ref_kind``（absolute/relative）、
    ``status``（ok/missing/ambiguous）、``target_indices``、``candidates``、
    ``detail``（中文定位说明）。
    """
    articles, para_article = build_structure(paras)
    results: list[dict] = []
    for idx, content in enumerate(paras):
        head = ARTICLE_HEAD_RE.match(content)
        seen: set[str] = set()
        for m in REF_RE.finditer(content):
            ref_text = m.group(0)
            # 段落开头的「第X条」是条文编号本身而非引用，跳过
            if head and m.end() <= head.end():
                continue
            # 《其他法规》第X条 属外部引用，不在本检查范围内
            if m.start() > 0 and content[m.start() - 1] == "》":
                continue
            if ref_text in seen:  # 同一段落内相同引用只记录首次出现
                continue
            seen.add(ref_text)
            gd = m.groupdict()
            if gd.get("range_from") is not None:
                r = _resolve_range(gd["range_from"], gd["range_to"], articles)
            elif gd.get("tiao") is not None:
                r = _resolve_absolute(gd["tiao"], gd.get("kuan"), gd.get("xiang"),
                                      articles, paras)
            else:
                r = _resolve_relative(ref_text, m, idx, articles, para_article)
            results.append({
                "source_idx": idx,
                "ref_text": ref_text,
                "position": m.start(),
                "ref_kind": r["kind"],
                "status": r["status"],
                "target_indices": r["targets"],
                "candidates": r["candidates"],
                "detail": r["detail"],
            })
    return results
