"""新版条款引用检查：扫描、解析与定位。

仅针对**新版**文本（旧版引用是否成立不在本模块范围）。

流程（可解释、可复现）：

1. ``extract_mentions`` 用一个有序交替的正则在每个新版段落中切出引用 token，
   再把相邻 token 组合成完整引用链，如 ``第十条第二款第（一）项``：
   - 绝对指代：``第十条`` / ``第10条`` / ``第十条第二款`` …
   - 相对指代：``本条``、``本条前款``、``本条第N款``、``前款``、``上一款``、
     ``前两款``、``前条``、``上一条``、裸 ``第N款``（按本条理解）…
   - 数字同时支持中文（十、二十三）与阿拉伯数字（含全角１０）。
2. ``build_structure`` 按行解析新版段落，建立 条/款/项 结构：
   ``第X条`` 开头的行为条标题（条的第一行即第一款），其后的普通行依次为
   第二款、第三款……；以 ``（一）`` / ``1.`` 开头的行属于上一款的项。
3. ``resolve`` 把每条引用按结构定位到**唯一**目标：
   - 恰好一个目标 → ``ok``；
   - 找不到任何目标 → ``missing``（缺失）；
   - 存在多个候选（重号条款、同号项分属多款等）→ ``ambiguous``（歧义），
     保留全部候选，**绝不静默选取**。

范围引用（如第三款中的``前两款``）合法地指向多个结构单元，状态仍为 ``ok``，
目标单元列表会包含多款。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ---------------- 数字解析 ----------------

_NUM_SRC = r"0-9０-９零〇一二两三四五六七八九十百千万"
NUM_TOKEN = f"[{_NUM_SRC}]+"

_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_SMALL_UNIT = {"十": 10, "百": 100, "千": 1000}


def cn_to_int(s: str) -> int | None:
    """中文数字转 int，支持到亿级；无法解析返回 None。"""
    total = 0          # 万/亿 以上的累计
    section = 0        # 当前小节（十/百/千）
    num: int | None = None
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch in _CN_SMALL_UNIT:
            section += (num if num else 1) * _CN_SMALL_UNIT[ch]
            num = None
        elif ch == "万":
            total += (section + (num or 0)) * 10000
            section, num = 0, None
        elif ch == "亿":
            total = (total + section + (num or 0)) * 100000000
            section, num = 0, None
        else:
            return None
    return total + section + (num or 0)


def parse_num(s: str) -> int | None:
    """引用中的数字 → int；支持阿拉伯（含全角）与中文数字。"""
    s = unicodedata.normalize("NFKC", s)
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return None
    return cn_to_int(s)


_CN_DIGITS = "零一二三四五六七八九"


def int_to_cn(n: int) -> str:
    """int → 中文数字（用于目标定位标签，如 10 -> 十）。"""
    if n < 0:
        return str(n)
    if n == 0:
        return "零"

    def sec4(g: int) -> str:
        ds = (g // 1000, (g // 100) % 10, (g // 10) % 10, g % 10)
        units = ("千", "百", "十", "")
        out, pending = "", False
        for d, u in zip(ds, units):
            if d:
                if pending:
                    out += "零"
                    pending = False
                out += _CN_DIGITS[d] + u
            elif out:
                pending = True
        return out

    groups: list[int] = []
    v = n
    while v:
        groups.append(v % 10000)
        v //= 10000
    names = ("", "万", "亿", "万亿")
    out = ""
    for gi in range(len(groups) - 1, -1, -1):
        g = groups[gi]
        if g:
            if out and g < 1000:
                out += "零"
            out += sec4(g) + names[gi]
        elif out:
            out += "零"
    out = re.sub("零+", "零", out).rstrip("零")
    if out.startswith("一十"):  # 10 -> 十（而非一十）
        out = out[1:]
    return out


# ---------------- 结构解析（条 / 款 / 项） ----------------

ART_HEAD_RE = re.compile(rf"^\s*第(?P<num>{NUM_TOKEN})条(?!之一)")
# 行首显式款号：要求款号后为空白或行尾（“第一款为……”这类正文不算结构标签）
KUAN_LABEL_RE = re.compile(rf"(?m)^\s*第(?P<num>{NUM_TOKEN})款(?=\s|$)")
# 行首的项标记：（一）/ (1) / 1. / 1、
_XIANG_LINE_RE = re.compile(
    rf"^\s*(?:[（(](?P<a>{NUM_TOKEN})[）)]|(?P<b>[0-9０-９]{{1,2}})[\.、)])\s*"
)


@dataclass
class LineInfo:
    para: int
    start: int
    end: int
    text: str
    art_no: int | None = None       # 所在条号
    kuan_no: int | None = None      # 所在款号（条标题行为第 1 款）
    xiang_no: int | None = None    # 项号（仅项行）
    is_head: bool = False


@dataclass
class KuanUnit:
    art_no: int
    kuan_no: int
    para: int
    line_start: int
    line_end: int
    text: str                       # 款文（含所属各项）
    xiangs: dict[int, "XiangUnit"] = field(default_factory=dict)
    head_only: bool = False         # 仅含条标题引导句（显式“第一款”出现前）


@dataclass
class XiangUnit:
    art_no: int
    kuan_no: int
    xiang_no: int
    para: int
    line_start: int
    line_end: int
    text: str


@dataclass
class Structure:
    paragraphs: list[str]
    lines: list[LineInfo]
    art_paras: dict[int, list[int]]              # 条号 → 归属段落（含续段）
    art_head_paras: dict[int, list[int]]         # 条号 → 标题所在段落（用于重号判歧义）
    kuans: dict[int, list[KuanUnit]]             # 条号 → 款（按款号）
    para_art: dict[int, int | None]              # 段落 → 条号（归属，含续段）


def _para_lines(para_idx: int, text: str):
    """按 \\n 切行并返回 (start, end, text)，end 为相对段落内容的偏移。"""
    out = []
    pos = 0
    for line in text.split("\n"):
        yield pos, pos + len(line), line
        pos += len(line) + 1


def build_structure(paragraphs: list[str]) -> Structure:
    """建立条/款/项结构。

    - 条：行首出现 ``第X条`` 即开启新条；无标题的续段归属于上一条，
      第一条之前的段落为“结构外”前言。
    - 款：条标题行计为第 1 款；其后每个非项行递增一款；项行属于上一款。
    """
    lines: list[LineInfo] = []
    art_paras: dict[int, list[int]] = {}
    art_head_paras: dict[int, list[int]] = {}
    current_art: int | None = None
    # 每个段落归属的条
    para_art: dict[int, int | None] = {}

    # 先记录条归属与行元数据
    for pi, ptext in enumerate(paragraphs):
        para_art.setdefault(pi, None)
        first = True
        for start, end, line in _para_lines(pi, ptext):
            if not line.strip():
                continue
            m = ART_HEAD_RE.match(line)
            info = LineInfo(para=pi, start=start, end=end, text=line.strip())
            if m:
                current_art = parse_num(m.group("num"))
                if current_art is not None:
                    art_paras.setdefault(current_art, [])
                    art_head_paras.setdefault(current_art, []).append(pi)
                    if pi not in art_paras[current_art]:
                        art_paras[current_art].append(pi)
                    para_art[pi] = current_art
                    info.is_head = True
            elif first and para_art[pi] is None and current_art is not None:
                # 无标题续段（显式款号行、项行、普通续段）归属于上一条
                para_art[pi] = current_art
                if pi not in art_paras.get(current_art, []):
                    art_paras.setdefault(current_art, []).append(pi)
            info.art_no = para_art[pi]
            lines.append(info)
            first = False

    # 再按条归集款/项（按段落与行顺序）
    kuans: dict[int, list[KuanUnit]] = {}
    kuan_index: dict[tuple[int, int], KuanUnit] = {}
    current_kuan: KuanUnit | None = None

    def open_kuan(art: int, no: int, info: LineInfo) -> KuanUnit:
        existing = kuan_index.get((art, no))
        if existing is not None:
            if existing.head_only:
                # 跨段落显式“第一款”：标题引导句仅作结构引导，款正文改挂该段
                existing.head_only = False
                existing.para = info.para
                existing.line_start = info.start
                existing.line_end = info.end
                existing.text = info.text
            else:
                existing.text += "\n" + info.text
                existing.line_end = info.end
            return existing
        ku = KuanUnit(art_no=art, kuan_no=no, para=info.para,
                      line_start=info.start, line_end=info.end, text=info.text)
        kuans.setdefault(art, []).append(ku)
        kuan_index[(art, no)] = ku
        return ku

    for info in lines:
        art = info.art_no
        if art is None:
            continue
        xm = _XIANG_LINE_RE.match(info.text)
        km = KUAN_LABEL_RE.match(info.text) if not xm else None
        if info.is_head:
            current_kuan = open_kuan(art, 1, info)
            current_kuan.head_only = True
            info.kuan_no = 1
        elif xm:
            xraw = xm.group("a") or xm.group("b")
            xno = parse_num(xraw)
            if current_kuan is not None and xno is not None and xno not in current_kuan.xiangs:
                xu = XiangUnit(art_no=art, kuan_no=current_kuan.kuan_no,
                               xiang_no=xno, para=info.para,
                               line_start=info.start, line_end=info.end,
                               text=info.text)
                current_kuan.xiangs[xno] = xu
                current_kuan.text += "\n" + info.text
                current_kuan.line_end = info.end
                current_kuan.head_only = False
                info.kuan_no = current_kuan.kuan_no
                info.xiang_no = xno
        elif km:
            no = parse_num(km.group("num"))
            if no is not None:
                current_kuan = open_kuan(art, no, info)
                info.kuan_no = no
        elif current_kuan is not None and info.para == current_kuan.para:
            # 同段内的普通续行属于当前款正文，不另开一款
            current_kuan.text += "\n" + info.text
            current_kuan.line_end = info.end
            info.kuan_no = current_kuan.kuan_no
        else:
            next_no = max((k.kuan_no for k in kuans.get(art, [])), default=0) + 1
            current_kuan = open_kuan(art, next_no, info)
            info.kuan_no = next_no

    # 保持按款号有序（显式款号可能乱序出现）
    for art in kuans:
        kuans[art].sort(key=lambda k: k.kuan_no)

    return Structure(paragraphs=paragraphs, lines=lines, art_paras=art_paras,
                     art_head_paras=art_head_paras, kuans=kuans, para_art=para_art)


def line_at(struct: Structure, para: int, offset: int) -> LineInfo | None:
    """定位某段落内字符偏移所在的行（条/款/项归属）。"""
    best: LineInfo | None = None
    for ln in struct.lines:
        if ln.para != para:
            continue
        if ln.start <= offset < max(ln.end, ln.start + 1):
            return ln
        best = ln
    return best


# ---------------- 引用 token 切分与组合 ----------------
#
# 同名分支按长到短排列；“本条”加负先行，避免把“本条例”误当作引用。
TOKEN_RE = re.compile(
    rf"第(?P<art>{NUM_TOKEN})条"
    rf"|(?P<thisart>本条)(?![款例])"
    rf"|(?P<preart>上一条|前一条|前条)"
    rf"|(?P<prekuan>前两款|[前上]一款|前款|上款)"
    rf"|第[（(](?P<xiangp>{NUM_TOKEN})[）)]项"
    rf"|第(?P<kuan>{NUM_TOKEN})款"
    rf"|第(?P<xiang>{NUM_TOKEN})项"
)

# 引用类型 → 中文展示名
REF_TYPE_LABEL = {
    "absolute_article": "绝对条引用（第X条）",
    "article_kuan": "条款引用（第X条第N款）",
    "article_kuan_xiang": "条款项引用",
    "article_xiang": "条项引用（第X条第N项）",
    "this_article": "本条引用",
    "this_kuan": "本条款引用（本条第N款）",
    "this_kuan_xiang": "本条款项引用",
    "this_previous_kuan": "本条前款引用",
    "this_previous_kuan_xiang": "本条前款项引用",
    "this_article_xiang": "本条项引用（本条第N项）",
    "previous_article": "前条引用",
    "previous_article_kuan": "前条款引用",
    "previous_kuan": "前款引用",
    "previous_kuan_xiang": "前款项引用",
    "current_kuan": "本条款引用（第N款）",
    "current_kuan_xiang": "本款项引用",
    "current_xiang": "本款项引用（第N项）",
}
REF_TYPES = tuple(REF_TYPE_LABEL)


@dataclass
class Token:
    kind: str           # art / thisart / preart / prekuan / kuan / xiang / xiangp
    num: int | None
    start: int
    end: int


def _tokens(text: str) -> list[Token]:
    out: list[Token] = []
    for m in TOKEN_RE.finditer(text):
        kind = m.lastgroup
        num = parse_num(m.group(kind)) if kind in ("art", "kuan", "xiang", "xiangp") else None
        out.append(Token(kind, num, m.start(), m.end()))
    return out


def _adjacent(text: str, a: Token, b: Token) -> bool:
    """token 间只允许零宽连接（条第/款第 形式上紧邻）；空格断开引用链。"""
    return text[a.end:b.start] == ""


@dataclass
class Mention:
    para: int
    start: int
    end: int
    text: str
    ref_type: str
    art_no: int | None = None      # 绝对条号（前条为本条号-1，解析时确定）
    kuan_no: int | None = None
    xiang_no: int | None = None
    relative: str | None = None    # this / preart / prekuan / current_kuan ...
    prev_count: int = 1            # 前 N 款（范围引用）


def extract_mentions(paragraphs: list[str]) -> list[Mention]:
    """切出全部引用。

    结构标签不计为引用：条标题开头的裸 ``第X条``、行首的显式款号
    ``第一款``（款号后为空白/行尾）属于结构，先剔除再做引用链组合。
    """
    mentions: list[Mention] = []
    for pi, ptext in enumerate(paragraphs):
        head = ART_HEAD_RE.match(ptext)
        toks = [t for t in _tokens(ptext) if not _is_structural_token(pi, ptext, head, t)]
        i = 0
        while i < len(toks):
            t = toks[i]
            chain = [t]
            j = i + 1
            while j < len(toks) and _adjacent(ptext, chain[-1], toks[j]):
                nxt = toks[j]
                last = chain[-1].kind
                ok = False
                if last == "art" and nxt.kind in ("kuan", "xiang", "xiangp"):
                    ok = True
                elif last == "thisart" and nxt.kind in ("prekuan", "kuan", "xiang", "xiangp"):
                    ok = True
                elif last == "preart" and nxt.kind in ("kuan", "xiang", "xiangp"):
                    ok = True
                elif last == "prekuan" and nxt.kind in ("xiang", "xiangp"):
                    ok = True
                elif last == "kuan" and nxt.kind in ("xiang", "xiangp"):
                    ok = True
                if not ok:
                    break
                chain.append(nxt)
                j += 1
            i = j
            m = _build_mention(pi, ptext, chain)
            if m is not None:
                mentions.append(m)
    return mentions


def _is_structural_token(pi: int, ptext: str, head: re.Match | None, t: Token) -> bool:
    """条标题自指与行首显式款/项标签属于结构，不是引用。"""
    if t.kind == "art" and head is not None \
            and t.start == head.start("num") - 1 and t.end == head.end():
        return True
    line_start = ptext.rfind("\n", 0, t.start) + 1
    prefix = ptext[line_start:t.start]
    if prefix.strip() != "":
        return False
    tail = ptext[t.end:t.end + 1]
    at_boundary = tail in ("", "\n", " ", "　", "\t", "\r")
    if t.kind == "kuan" and at_boundary:
        return True
    if t.kind in ("xiang", "xiangp"):
        # 行首“第（一）项 / 第一项”作为项标题（后接空白），是结构
        if at_boundary:
            return True
    return False


def _build_mention(pi: int, ptext: str, chain: list[Token]) -> Mention | None:
    kinds = [t.kind for t in chain]
    nums = {t.kind: t.num for t in chain}
    start, end = chain[0].start, chain[-1].end
    raw = ptext[start:end]
    xk = "xiangp" if "xiangp" in kinds else ("xiang" if "xiang" in kinds else None)
    xno = nums.get(xk) if xk else None
    kno = nums.get("kuan")
    ano = nums.get("art")

    def mk(rt: str, relative: str | None = None, prev_count: int = 1) -> Mention:
        return Mention(pi, start, end, raw, rt, art_no=ano, kuan_no=kno,
                       xiang_no=xno, relative=relative, prev_count=prev_count)

    k0 = kinds[0]
    if k0 == "art":
        if len(chain) == 1:
            return mk("absolute_article")
        if "kuan" in kinds:
            return mk("article_kuan_xiang" if xk else "article_kuan")
        if xk:
            return mk("article_xiang")
    if k0 == "thisart":
        if len(chain) == 1:
            return mk("this_article", relative="this")
        if "prekuan" in kinds:
            tail_tok = chain[1]
            cnt = 2 if ptext[tail_tok.start:tail_tok.end] == "前两款" else 1
            return mk("this_previous_kuan_xiang" if xk else "this_previous_kuan",
                      relative="this_prekuan", prev_count=cnt)
        if "kuan" in kinds:
            return mk("this_kuan_xiang" if xk else "this_kuan", relative="this")
        if xk:
            # 本条第N项：按整条范围查找项（可能跨款 -> 歧义）
            m = mk("this_kuan", relative="this")
            m.kuan_no = None
            m.xiang_no = xno
            m.ref_type = "this_article_xiang"
            return m
    if k0 == "preart":
        if "kuan" in kinds:
            return mk("previous_article_kuan", relative="preart")
        return mk("previous_article", relative="preart")
    if k0 == "prekuan":
        tail = ptext[chain[0].start:chain[0].end]
        cnt = 2 if tail == "前两款" else 1
        return mk("previous_kuan_xiang" if xk else "previous_kuan",
                  relative="prekuan", prev_count=cnt)
    if k0 == "kuan":
        return mk("current_kuan_xiang" if xk else "current_kuan",
                  relative="current_kuan")
    if k0 in ("xiang", "xiangp"):
        # 裸第N项：按本款的项理解
        return Mention(pi, start, end, raw, "current_xiang",
                       xiang_no=chain[0].num, relative="current_xiang")
    return None


# ---------------- 定位 ----------------

STATUS_LABEL = {"ok": "正常", "missing": "缺失", "ambiguous": "歧义"}


@dataclass
class TargetUnit:
    para_idx: int
    label: str
    excerpt: str
    kuan_no: int | None = None
    xiang_no: int | None = None


@dataclass
class Result:
    status: str
    reason: str
    targets: list[TargetUnit] = field(default_factory=list)
    candidates: list[TargetUnit] = field(default_factory=list)

    @property
    def target_label(self) -> str:
        return "、".join(t.label for t in self.targets)

    @property
    def target_excerpt(self) -> str:
        return "\n\n".join(t.excerpt for t in self.targets)


def _art_label(n: int) -> str:
    return f"第{int_to_cn(n)}条"


def _kuan_label(art: int, k: int) -> str:
    return f"{_art_label(art)}第{int_to_cn(k)}款"


def _xiang_label(art: int, k: int | None, x: int) -> str:
    pre = _kuan_label(art, k) if k else _art_label(art)
    return f"{pre}第（{int_to_cn(x)}）项"


def _article_unit(struct: Structure, art: int, para: int) -> TargetUnit:
    # 目标原文聚合该条归属的全部段落（条标题＋续段/显式款段）
    paras = struct.art_paras.get(art, []) or [para]
    excerpt = "\n\n".join(struct.paragraphs[p] for p in paras)
    return TargetUnit(para_idx=para, label=_art_label(art), excerpt=excerpt)


def _resolve_article(struct: Structure, art: int | None, raw_label: str,
                     reason_missing: str) -> tuple[str, list[TargetUnit], list[TargetUnit], str]:
    """返回 (status, targets, candidates, reason)。

    重号判定以“条标题”出现次数为准：同一条下跨多个段落（条标题＋显式款号段）
    是正常结构，不算重号。
    """
    if art is None:
        return "missing", [], [], "引用编号无法解析为数字"
    heads = struct.art_head_paras.get(art, [])
    if len(heads) == 0:
        return "missing", [], [], reason_missing
    if len(heads) > 1:
        cands = [TargetUnit(para_idx=p,
                            label=f"{_art_label(art)}（新版第 {p + 1} 段，编号重复）",
                            excerpt=struct.paragraphs[p]) for p in heads]
        return ("ambiguous", [], cands,
                f"新版中存在 {len(heads)} 处{_art_label(art)}标题（段落 "
                f"{'、'.join(str(p + 1) for p in heads)}），无法确定唯一目标")
    p = heads[0]
    return "ok", [_article_unit(struct, art, p)], [], f"唯一定位至{_art_label(art)}（新版第 {p + 1} 段）"


def _find_kuan(struct: Structure, art: int, k: int) -> KuanUnit | None:
    for ku in struct.kuans.get(art, []):
        if ku.kuan_no == k:
            return ku
    return None


def _kuan_target(ku: KuanUnit, struct: Structure) -> TargetUnit:
    return TargetUnit(para_idx=ku.para,
                      label=_kuan_label(ku.art_no, ku.kuan_no),
                      excerpt=ku.text, kuan_no=ku.kuan_no)


def _xiang_target(xu: XiangUnit) -> TargetUnit:
    return TargetUnit(para_idx=xu.para, label=_xiang_label(xu.art_no, xu.kuan_no, xu.xiang_no),
                      excerpt=xu.text, kuan_no=xu.kuan_no, xiang_no=xu.xiang_no)


def _resolve_xiang_in_kuan(ku: KuanUnit, x: int) -> tuple[str, list, str]:
    xu = ku.xiangs.get(x)
    if xu is None:
        return "missing", [], f"{_kuan_label(ku.art_no, ku.kuan_no)}下未找到第（{int_to_cn(x)}）项"
    return "ok", [_xiang_target(xu)], f"唯一定位至{_xiang_label(ku.art_no, ku.kuan_no, x)}"


def _resolve_xiang_in_article(art: int, x: int, struct: Structure) -> tuple[str, list, list, str]:
    """仅给项号时在整条范围内找；分属多款则歧义。"""
    xhits = [xu for ku in struct.kuans.get(art, []) for xu in ku.xiangs.values()
             if xu.xiang_no == x]
    if not xhits:
        return "missing", [], [], f"{_art_label(art)}下未找到第（{int_to_cn(x)}）项"
    if len({(u.para, u.line_start) for u in xhits}) > 1:
        cands = [_xiang_target(u) for u in xhits]
        return ("ambiguous", [], cands,
                f"{_art_label(art)}中有 {len(xhits)} 个第（{int_to_cn(x)}）项"
                "（分属不同款），无法确定唯一目标")
    u = xhits[0]
    return "ok", [_xiang_target(u)], [], f"唯一定位至{_xiang_label(art, u.kuan_no, u.xiang_no)}"


def resolve(men: Mention, struct: Structure) -> Result:
    """按新版结构定位单条引用；不静默选取候选。"""
    src = line_at(struct, men.para, men.start)
    src_art = src.art_no if src else struct.para_art.get(men.para)
    src_kuan = src.kuan_no if src else None
    p1 = men.para + 1

    # ---- 绝对条 ----
    if men.ref_type == "absolute_article":
        st, tg, cands, why = _resolve_article(
            struct, men.art_no, _art_label(men.art_no or 0),
            f"新版中未找到{_art_label(men.art_no or 0)}（引用位于新版第 {p1} 段）")
        return Result(st, why, tg, cands)

    # ---- 本条 ----
    if men.ref_type == "this_article":
        if src_art is None:
            return Result("missing", "来源段落不在任何“第X条”结构内，无法确定“本条”所指")
        st, tg, cands, why = _resolve_article(
            struct, src_art, _art_label(src_art), f"来源所在的{_art_label(src_art)}在新版中缺失")
        return Result(st, why, tg, cands)

    # ---- 本条第N项（按整条范围找项）----
    if men.ref_type == "this_article_xiang":
        if src_art is None:
            return Result("missing", "来源段落不在任何“第X条”结构内，无法按本条定位项")
        xst, xtg, xcands, xwhy = _resolve_xiang_in_article(src_art, men.xiang_no or 0, struct)
        return Result(xst, xwhy, xtg, xcands)

    # ---- 前条 ----
    if men.ref_type == "previous_article":
        if src_art is None:
            return Result("missing", "来源段落不在任何“第X条”结构内，无法确定“前条”所指")
        target_no = src_art - 1
        if target_no < 1:
            return Result("missing", f"来源为{_art_label(src_art)}，不存在前一条")
        st, tg, cands, why = _resolve_article(
            struct, target_no, _art_label(target_no),
            f"{_art_label(src_art)}的前一条应为{_art_label(target_no)}，但新版中未找到")
        return Result(st, why, tg, cands)

    # ---- 第X条第N款 / 前条第N款 ----
    if men.ref_type in ("article_kuan", "article_kuan_xiang", "article_xiang",
                        "previous_article_kuan"):
        if men.ref_type == "previous_article_kuan":
            if src_art is None:
                return Result("missing", "来源段落不在条款结构内，无法确定“前条”所指")
            art = src_art - 1
            if art < 1:
                return Result("missing", f"来源为{_art_label(src_art)}，不存在前一条")
        else:
            art = men.art_no
        st, tg, cands, why = _resolve_article(
            struct, art, _art_label(art or 0),
            f"新版中未找到{_art_label(art or 0)}，无法定位其第 {men.kuan_no or men.xiang_no} 款/项")
        if st != "ok":
            return Result(st, why, tg, cands)
        kuans = struct.kuans.get(art, [])
        # 仅给项号：在整条范围内找
        if men.ref_type == "article_xiang":
            xst, xtg, xcands, xwhy = _resolve_xiang_in_article(art, men.xiang_no or 0, struct)
            return Result(xst, xwhy, xtg, xcands)
        ku = _find_kuan(struct, art, men.kuan_no or 0)
        if ku is None:
            return Result("missing",
                          f"{_art_label(art)}只有 {len(kuans)} 款，"
                          f"不存在第{int_to_cn(men.kuan_no or 0)}款")
        if men.ref_type == "article_kuan":
            return Result("ok", f"唯一定位至{_kuan_label(art, ku.kuan_no)}", [_kuan_target(ku, struct)])
        xst, xtg, xwhy = _resolve_xiang_in_kuan(ku, men.xiang_no or 0)
        return Result(xst, xwhy, xtg)

    # ---- 本条第N款（含裸第N款）----
    if men.ref_type in ("this_kuan", "this_kuan_xiang",
                        "current_kuan", "current_kuan_xiang"):
        if src_art is None:
            return Result("missing", "来源段落不在任何“第X条”结构内，无法按本条定位款/项")
        ku = _find_kuan(struct, src_art, men.kuan_no or 0)
        label = {
            "this_kuan": "本条", "this_kuan_xiang": "本条",
            "current_kuan": "本款所在条", "current_kuan_xiang": "本款所在条",
        }[men.ref_type]
        if ku is None:
            return Result("missing",
                          f"{label}{_art_label(src_art)}只有 {len(struct.kuans.get(src_art, []))} 款，"
                          f"不存在第{int_to_cn(men.kuan_no or 0)}款")
        if men.ref_type in ("this_kuan", "current_kuan"):
            return Result("ok", f"唯一定位至{_kuan_label(src_art, ku.kuan_no)}",
                          [_kuan_target(ku, struct)])
        xst, xtg, xwhy = _resolve_xiang_in_kuan(ku, men.xiang_no or 0)
        return Result(xst, xwhy, xtg)

    # ---- 裸第N项（本款）----
    if men.ref_type == "current_xiang":
        if src_art is None or src_kuan is None:
            return Result("missing", "来源不在明确的条/款结构内，无法按“本款第N项”定位")
        ku = _find_kuan(struct, src_art, src_kuan)
        if ku is None:
            return Result("missing", "来源所在款结构缺失")
        xst, xtg, xwhy = _resolve_xiang_in_kuan(ku, men.xiang_no or 0)
        return Result(xst, xwhy, xtg)

    # ---- 本条前款 / 前款 / 上一款 / 前两款 ----
    if men.ref_type in ("this_previous_kuan", "this_previous_kuan_xiang",
                        "previous_kuan", "previous_kuan_xiang"):
        if src_art is None:
            return Result("missing", "来源段落不在任何“第X条”结构内，无法按本条定位上一款")
        if src_kuan is None:
            return Result("missing", f"无法确定来源在{_art_label(src_art)}中的款序，不能解析上一款")
        cnt = men.prev_count
        first = src_kuan - cnt
        if first < 1:
            return Result("missing",
                          f"来源位于{_art_label(src_art)}第{int_to_cn(src_kuan)}款，"
                          f"之前不足 {cnt} 款，引用不成立")
        units = [_find_kuan(struct, src_art, k) for k in range(first, src_kuan)]
        units = [u for u in units if u is not None]
        if len(units) != cnt:
            return Result("missing",
                          f"{_art_label(src_art)}款结构不完整，未能集齐所引前 {cnt} 款")
        if men.xiang_no is not None:
            if cnt != 1:
                return Result("missing", "“前多款第N项”指向不明确，无法定位")
            xst, xtg, xwhy = _resolve_xiang_in_kuan(units[0], men.xiang_no)
            return Result(xst, xwhy, xtg)
        targets = [_kuan_target(u, struct) for u in units]
        labels = "、".join(t.label for t in targets)
        return Result("ok", f"唯一定位至{labels}（共 {cnt} 款）", targets)

    return Result("missing", f"暂不支持的引用类型：{men.ref_type}")


# ---------------- 扫描入口 ----------------

def scan_citations(paragraphs: list[str]) -> list[dict]:
    """扫描新版段落，返回引用检查记录（dict，可直接落库）。"""
    struct = build_structure(paragraphs)
    mentions = extract_mentions(paragraphs)
    records: list[dict] = []
    for order, men in enumerate(mentions):
        res = resolve(men, struct)
        src = line_at(struct, men.para, men.start)
        src_art = src.art_no if src else None
        src_kuan = src.kuan_no if src else None
        src_xiang = src.xiang_no if src else None
        loc_parts = [f"新版第 {men.para + 1} 段"]
        if src_art is not None:
            loc_parts.append(_art_label(src_art))
            if src_kuan is not None:
                loc_parts.append(f"第{int_to_cn(src_kuan)}款")
            if src_xiang is not None:
                loc_parts.append(f"第（{int_to_cn(src_xiang)}）项")
        else:
            loc_parts.append("（条款结构外）")
        records.append({
            "order_idx": order,
            "source_para_idx": men.para,
            "source_start": men.start,
            "source_end": men.end,
            "mention_text": men.text,
            "ref_type": men.ref_type,
            "article_no": men.art_no if men.ref_type.startswith(("absolute", "article")) else None,
            "kuan_no": men.kuan_no,
            "xiang_no": men.xiang_no,
            "relative": men.relative,
            "status": res.status,
            "targets": [{"para_idx": t.para_idx, "label": t.label,
                         "excerpt": t.excerpt, "kuan_no": t.kuan_no,
                         "xiang_no": t.xiang_no} for t in res.targets],
            "candidates": [{"para_idx": c.para_idx, "label": c.label,
                            "excerpt": c.excerpt} for c in res.candidates],
            "target_label": res.target_label,
            "target_excerpt": res.target_excerpt,
            "source_location": " · ".join(loc_parts),
            "reason": res.reason,
        })
    return records
