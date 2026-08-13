from __future__ import annotations

import re
from typing import List, Dict, Optional, Tuple


# ==================== 正则定义（候选匹配用，置信度打分决定最终是否采纳） ====================

# 高置信度：标准「第X章/回/节/卷/集/部/篇 + 标题」—— 匹配后还需通过规则过滤
_P1 = re.compile(
    r"^\s*第\s*([零一二三四五六七八九十百千万〇两\d]+)\s*([章节回卷集部篇])\s*[::．.:：\-—_ 　]*\s*(.*)$"
)

# 中置信度：Chapter N + 标题
_P2 = re.compile(
    r"^\s*Chapter\s*(\d+)[::.\-—_ 　]*\s*(.*)$", re.IGNORECASE
)

# 中置信度：序号数字 + 顿号/点/句号 + 标题 （常见于纯数字编号 1、楔子 / 1. 楔子 / 1.出手）
# 注意分隔符后用 \s* 而非 \s+，兼容顿号后直接接汉字（无空格）的常见网文排版
_P3 = re.compile(
    r"^\s*(\d{1,5})\s*[、\.．]\s*(.+)$"
)

# 低置信度：仅 3~4 位纯数字补零开头 + 空白 + 标题 （如 001 楔子 / 002、前言）
_P4 = re.compile(
    r"^\s*(\d{3,4})\s+(.+)$"
)

# 特殊章节：无前缀的楔子/序/前言/引子/尾声/后记/终章/番外等
_SPECIAL_PATTERNS = [
    re.compile(r"^\s*(序章?|序言?|前言|楔子|引子|序幕|开篇|写在前面)\s*[::．.\-—_]*\s*(.*)$"),
    re.compile(r"^\s*(尾声|后记|终章|结局|完本感言|新书感言|番外[篇零一二三四五六七八九十百千\d]*)\s*[::．.\-—_]*\s*(.*)$"),
]

# 必须忽略的行
SKIP_LINES_PATTERNS = [
    re.compile(r"^\s*本书由"),
    re.compile(r"^\s*更多更新"),
    re.compile(r"^\s*版权所有"),
    re.compile(r"^\s*===+"),
    re.compile(r"^\s*---+"),
    re.compile(r"^\s*\*\*\*+"),
    re.compile(r"^\s*书名\s*[:：]"),
    re.compile(r"^\s*作者\s*[:：]"),
    re.compile(r"^\s*简介\s*[:：]?$"),
]


def _cn_num_to_int(s: str) -> int:
    # 纯阿拉伯数字
    if s.isdigit():
        return int(s)

    digit_map = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
                 "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    mult_chars = ("十", "百", "千", "万")

    # 检测是否是纯位置数字表示法（不含十/百/千/万，如 "一〇"=10, "一一"=11, "一〇二"=102）
    has_multiplier = any(ch in mult_chars for ch in s)
    if not has_multiplier:
        result = 0
        for ch in s:
            if ch not in digit_map:
                return -1
            result = result * 10 + digit_map[ch]
        return result if result > 0 else -1

    # 标准混合计数法（带十/百/千/万，如 "十一"=11, "一百二十"=120）
    cn_map = {
        **{k: v for k, v in digit_map.items()},
        "十": 10, "百": 100, "千": 1000, "万": 10000,
    }
    try:
        total = 0
        current = 0
        for ch in s:
            if ch not in cn_map:
                return -1
            v = cn_map[ch]
            if v >= 10:
                if current == 0:
                    current = 1
                total += current * v
                current = 0
            else:
                current = v
        total += current
        return total if total > 0 else -1
    except Exception:
        return -1


def _line_is_empty(line: str) -> bool:
    return len(line.strip()) == 0


def _looks_like_sentence_end(text: str) -> bool:
    """
    只有「标题很长 + 句号/问号/感叹号/省略号收尾」才判定为正文句特征。
    标题里的括号（上/中/下）、书名号《》、引号「」“”、以及短标题的！？（如「钥匙GET！」「觉醒？」）
    都是网文章节标题常态，**绝不惩罚**。
    """
    t = text.strip()
    if not t:
        return False
    if len(t) <= 30:
        # 短标题：无论什么结尾都不算正文句（可能是 GET！/？/括弧型 等典型标题写法）
        return False
    return bool(re.search(r"[。？！?!…]$", t))


def _count_chars(text: str) -> int:
    """统计标题中可见字符的大致长度（去掉前后空白）"""
    return len(text.strip())


# ==================== 多层匹配：候选 -> 打分 -> 判定（用整数分避免浮点误差）====================

def _score_candidate_int(
    lines: List[str],
    line_no: int,
    pat_source: str,
    num: int,
    title_text: str,
    last_accepted_num: Optional[int],
) -> Tuple[int, str]:
    """
    整数打分制（满分100），避免浮点误差。
    返回 (score 0~100, 理由简述)
    一般阈值：P1/P1V/P2 >= 55，P3 >= 65，P4 >= 60，特殊章节 S >= 45
    """
    s = 0
    reasons = []

    raw_line = lines[line_no]

    # 1) 长度
    tl = _count_chars(title_text)
    rl = _count_chars(raw_line)
    if tl <= 60 and rl <= 80:
        s += 30; reasons.append("短标题")
    elif tl <= 100 and rl <= 120:
        s += 15; reasons.append("中长度")
    else:
        s -= 30; reasons.append("过长")

    # 2) 句末标点惩罚（只针对长标题的句号类，不再误伤（上）/！？/引号等）
    if not _looks_like_sentence_end(title_text or raw_line):
        s += 15; reasons.append("无正文式句末")
    else:
        s -= 25; reasons.append("正文式句末")

    # 3) 序号单调性（多种豁免模式）
    is_special = pat_source.startswith("S")
    is_volume = (pat_source == "P1V")          # 第X卷/部/篇/集：卷号与章号维度不同，豁免
    is_weak_num = pat_source in ("P3", "P4")   # 弱编号格式（001 xx / 1、xx）：作者可能乱写，宽松处理

    if is_special or is_volume:
        s += 20
        if is_special: reasons.append("特章不受序号约束")
        else: reasons.append("卷部篇集豁免序号")
    elif is_weak_num:
        # P3/P4：弱编号格式不参与严格单调，只给轻微加分/小惩罚，避免回退/重复直接致死
        if last_accepted_num is None:
            if 0 < num <= 5000:
                s += 10; reasons.append("弱首号合理")
        else:
            if num > last_accepted_num:
                s += 10; reasons.append(f"弱号↑")
            elif num == last_accepted_num:
                # P4(001)之后P3(1、xxx) 常见混用，不惩罚
                s += 0; reasons.append("弱号重复(豁免)")
            else:
                # 轻微回退（弱编号不保证单调）：只小扣分
                s -= 15; reasons.append(f"弱号小回退")
    elif last_accepted_num is None:
        if 0 < num <= 5000:
            s += 15; reasons.append("首章合理序号")
    elif last_accepted_num < 0:
        if num > 0:
            s += 25; reasons.append(f"特章后首章↑")
    else:
        if num > last_accepted_num:
            s += 25; reasons.append(f"序号↑{last_accepted_num}→{num}")
        elif num == last_accepted_num:
            s -= 50; reasons.append(f"序号重复{num}")
        else:
            s -= 80; reasons.append(f"序号回退{last_accepted_num}→{num}")

    # 4) 上下文空行
    prev_empty = line_no == 0 or _line_is_empty(lines[line_no - 1])
    next_empty = line_no >= len(lines) - 1 or _line_is_empty(lines[line_no + 1])
    if prev_empty and next_empty:
        s += 25; reasons.append("上下皆空行")
    elif prev_empty or next_empty:
        s += 10; reasons.append("单侧空行")
    else:
        s -= 10; reasons.append("无空行")

    # 5) 正则来源差异
    if pat_source in ("P1", "P1V"):
        s += 10; reasons.append("标准第X章/卷")
    elif pat_source.startswith("S"):
        s += 20; reasons.append("特殊章节")
    elif pat_source == "P3":
        s -= 5
        if num > 200 and not (prev_empty or next_empty):
            s -= 20; reasons.append("大数字+P3(警惕)")
    elif pat_source == "P4":
        s -= 5

    # 6) P1 连续 +1 加分（典型长篇小说章节节奏）
    if pat_source == "P1" and last_accepted_num is not None and last_accepted_num > 0 and num == last_accepted_num + 1:
        s += 10; reasons.append("连续+1")

    return max(0, min(100, s)), "|".join(reasons)


_VOLUME_UNITS = ("卷", "部", "篇", "集")

def _find_candidate(lines: List[str], i: int, last_accepted_num: Optional[int]) -> Optional[Tuple[int, str, bool]]:
    """
    对第 i 行尝试所有正则，返回最佳 (num, title, count_in_chain) or None
    count_in_chain: 是否计入 last_num 主链（仅 P1(章/回/节) + P2 为 True，
                    P1V(卷/部/篇/集) / S(特殊) / P3 / P4 为 False，不污染主章号单调性）
    """
    line = lines[i]
    cands = []  # (score_int, num, title, count_in_chain)

    # P1: 第X章/回/节/卷/集/部/篇
    m = _P1.match(line)
    if m:
        num_raw, unit, title_text = m.group(1), m.group(2), (m.group(3) or "").strip()
        num = _cn_num_to_int(num_raw)
        if unit == "卷":
            num = num if num > 0 else -1
        if num > 0:
            if unit in _VOLUME_UNITS:
                pat = "P1V"
                count_in_chain = False
            else:
                pat = "P1"
                count_in_chain = True
            score, _ = _score_candidate_int(lines, i, pat, num, title_text, last_accepted_num)
            if score >= 55:
                title = title_text or f"第{num_raw}{unit}"
                cands.append((score, num, title, count_in_chain))

    # P2: Chapter N
    m = _P2.match(line)
    if m:
        num = int(m.group(1))
        title_text = (m.group(2) or "").strip()
        score, _ = _score_candidate_int(lines, i, "P2", num, title_text, last_accepted_num)
        if score >= 55:
            title = title_text or f"Chapter {num}"
            cands.append((score, num, title, True))

    # 特殊章节（楔子/序等）—— 使用**负虚拟序号**，避免和真实第1~N章冲突
    for idx, sp in enumerate(_SPECIAL_PATTERNS):
        m = sp.match(line)
        if m:
            head_text = m.group(1)
            rest_text = (m.group(2) or "").strip()
            if last_accepted_num is None or last_accepted_num >= 0:
                virtual_num = -1
            else:
                virtual_num = last_accepted_num - 1
            score, _ = _score_candidate_int(lines, i, f"S{idx}", virtual_num, f"{head_text} {rest_text}".strip(), last_accepted_num)
            if score >= 45:
                title = (head_text + (" " + rest_text if rest_text else "")).strip()
                cands.append((score, virtual_num, title, False))

    # P3: N、标题 （阈值更高）
    m = _P3.match(line)
    if m:
        num = int(m.group(1))
        title_text = (m.group(2) or "").strip()
        score, _ = _score_candidate_int(lines, i, "P3", num, title_text, last_accepted_num)
        if score >= 65:
            cands.append((score, num, title_text, False))

    # P4: 001 标题
    m = _P4.match(line)
    if m:
        num = int(m.group(1))
        title_text = (m.group(2) or "").strip()
        score, _ = _score_candidate_int(lines, i, "P4", num, title_text, last_accepted_num)
        if score >= 60:
            cands.append((score, num, title_text, False))

    if not cands:
        return None
    cands.sort(key=lambda x: x[0], reverse=True)
    _, num, title, count_in_chain = cands[0]
    return (num, title, count_in_chain)


def parse_chapters(raw_text: str) -> List[Dict]:
    # 预清洗：去除必须忽略的行
    all_lines = raw_text.splitlines()
    lines: List[str] = []
    for ln in all_lines:
        skip = False
        for pat in SKIP_LINES_PATTERNS:
            if pat.match(ln):
                skip = True
                break
        if not skip:
            lines.append(ln)

    if not lines:
        return [{
            "index": 1, "title": "全文", "content": "",
            "paragraphs": [], "line_range": [0, 0]
        }]

    # 阶段一：逐行扫描，标记 header[i] = (num, title) or None
    headers: List[Optional[Tuple[int, str]]] = [None] * len(lines)
    last_num: Optional[int] = None  # 主链章号（仅 P1章/回/节、P2 更新，卷/特殊/弱编号不污染）
    accepted_count = 0
    chain_count = 0  # 主链上已接受的章（回/节）数量，用于硬门槛判断

    for i in range(len(lines)):
        if not lines[i].strip():
            continue
        cand = _find_candidate(lines, i, last_num)
        if cand is None:
            continue
        num, title, count_in_chain = cand

        # 二次硬门槛：仅针对「主链严格章号」(count_in_chain=True)
        # 主链累计 3+ 章后不再允许 num<=last_num（正文引用特征）
        if count_in_chain and last_num is not None and last_num > 0 and num > 0 and num <= last_num and chain_count >= 3:
            continue

        headers[i] = (num, title)

        # 只有主链（章/回/节 + Chapter N）才更新 last_num
        if count_in_chain:
            if last_num is None:
                last_num = num
            elif num > last_num:
                last_num = num
            else:
                # 虽然通过了硬门槛但 num<=last_num（例如章节格式特殊跳号）
                # 仍然保留 last_num 不回退，防止后续连续主链判断出错
                pass
            chain_count += 1
        # 非主链（卷/特殊/弱编号）不更新 last_num，保持主链单调
        accepted_count += 1

    # 阶段二：按 headers 标记顺序组装章节
    chapters: List[Dict] = []
    cur_start = 0
    cur_title = "前言 / 楔子"

    def flush_content(end_line_exclusive: int, start_line: int):
        buf = [lines[li] for li in range(start_line, min(end_line_exclusive, len(lines)))]
        content = "\n".join(buf).strip()
        return content, [start_line, max(start_line, end_line_exclusive - 1)]

    next_index = 1  # 最终 index 严格按出现顺序自增，不依赖正则捕获的数字

    for i in range(len(lines)):
        h = headers[i]
        if h is None:
            continue
        num, title = h

        # 把当前标题之前的内容flush为"上一个章节"
        has_content = (i > cur_start)
        if has_content or chapters:
            content, rng = flush_content(i, cur_start)
            if content.strip() or chapters:
                chapters.append({
                    "index": next_index,
                    "title": cur_title,
                    "content": content,
                    "line_range": rng,
                })
                next_index += 1

        cur_title = title
        cur_start = i + 1  # 标题行本身不入正文

    # flush 最后一章
    if cur_start <= len(lines):
        content, rng = flush_content(len(lines), cur_start)
        if content.strip() or not chapters:
            chapters.append({
                "index": next_index,
                "title": cur_title,
                "content": content,
                "line_range": rng,
            })
            next_index += 1

    # 阶段三：后处理
    # 3a) 清理 index=1 的空前言/楔子
    cleaned_chapters = []
    for ch in chapters:
        if ch["index"] == 1 and ch["title"] == "前言 / 楔子" and len(ch["content"].strip()) < 20:
            continue
        cleaned_chapters.append(ch)
    chapters = cleaned_chapters

    # 3b) index 严格连续 1..N（保证任何异常情况都不影响前端）
    for new_idx, ch in enumerate(chapters, start=1):
        ch["index"] = new_idx

    if not chapters:
        return [{
            "index": 1, "title": "全文",
            "content": "\n".join(lines).strip(),
            "line_range": [0, max(0, len(lines) - 1)]
        }]

    return chapters


def chapter_to_paragraphs(content: str) -> List[str]:
    if not content:
        return []
    # 按换行拆分为非空段落；比正则 split(\n\s*\n|\n) 更快，且语义等价
    return [p.strip() for p in content.split("\n") if p.strip()]
