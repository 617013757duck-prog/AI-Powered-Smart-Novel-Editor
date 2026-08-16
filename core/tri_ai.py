from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .ollama_client import OllamaClient
from .external_client import ExternalAPIClient
from .chroma_memory import ChromaMemory
from config.settings import load_config, DATA_DIR


WRITER_SYSTEM = """你是一位资深网文编辑（Writer AI），负责根据用户的修改指令对小说段落进行精修、润色、扩写或改写。

核心原则：
1. 忠于原著的世界观、人物设定、剧情走向和风格基调
2. 参考用户提供的"相关记忆"和"设定摘要"保持前后一致，避免OOC（角色崩坏）
3. 语言自然流畅，避免AI腔；保持原著的叙事视角（第一/第三人称）
4. 对场景、对话、心理活动做合理扩写，不做无意义的水字数
5. 如果用户要求"精修AI生成内容"，重点修正：语句生硬、重复用词、逻辑跳步、人名地名笔误、动作不合理
6. 返回时直接输出修改后的段落，不要解释修改原因，不要加"修改如下"等前缀

输出格式：
- 若用户给了多个段落，请保持段落一一对应，用空行分隔
- 不要添加任何你自己的分析性文字，除非用户明确要求
"""

REVIEWER_SYSTEM = """你是一位严格的文学审校总监（Reviewer AI），负责独立检查小说内容的一致性问题。

你要重点检测：
1. OOC（角色崩坏）：人物行为、语气、价值观是否与之前的设定/记忆矛盾
2. 逻辑漏洞：时间线、因果关系、空间位置是否矛盾；道具/能力是否凭空出现或消失
3. 设定冲突：世界观规则、等级体系、人物关系是否与前文冲突
4. 细节错误：人名、地名、称号、物品名写错；数字/单位前后不一致
5. 文笔问题：重复用词、病句、过度形容词堆砌、对话不自然

输出格式（必须）：
- 若发现问题：使用JSON数组输出，每项为 {"severity":"high|medium|low","type":"OOC|逻辑|设定|细节|文笔","position":"第X段/第Y句","problem":"问题描述","suggestion":"修改建议"}
- 若未发现问题：输出 {"pass": true, "comment": "本段未检测到明显问题。"}
- 禁止输出JSON以外的内容。
"""

# ========== 世界书总结 Prompt 模板（小说转世界书，参考 Nika-Character-Studio 分类模板系统） ==========
# 每个模板都强制：content 使用总分结构（# 标题 + 1.2.3. 列举 + 先概括再 - 细分），严禁 **粗体字**。
SUMMARIZER_TEMPLATES = {
    "detailed": "📖 详细梳理版（推荐）：所有设定尽量完整详细，分条列举",
    "concise": "📌 简洁版：只保留核心设定与关键信息，每条精简",
    "storyline": "🎬 剧情推进版：侧重剧情事件、人物关系与时间线发展",
}

# 分类模板：每个分类给出 content 应包含的字段（输出时用 # 标题 + 1.2.3 列举）
WORLDBOOK_CATEGORY_TEMPLATES = {
    "角色": ["名称", "称号与相关别称", "性别", "MBTI", "年龄",
             "身份（含特定身份/形态/变身状态，如天使形态、水蜥蜴形态）",
             "外貌（存在不同时期变化或变体时，按时期分别叙述）",
             "背景经历（按时间线分条，如 - 第X章：…）",
             "性格", "技能", "重要事件", "弱点", "话语示例"],
    "地点": ["名称", "别称", "位置", "特征", "重要事件"],
    "组织": ["名称", "性质", "成员", "目标", "重要事件"],
    "物品": ["名称", "类型", "功能", "来源", "持有者"],
    "种族": ["名称", "特征", "能力", "栖息地", "与人类关系"],
    "世界观": ["世界规则", "力量体系", "历史背景", "地理", "重要设定"],
    "剧情": ["主线剧情", "支线剧情", "关键转折点", "伏笔与暗线"],
    "关系": ["双方", "关系性质", "发展过程"],
    "知识书": ["条目定义/说明", "相关细节", "影响与用途"],
}

_ALL_CATEGORIES = list(WORLDBOOK_CATEGORY_TEMPLATES.keys())


def _build_summarizer_system(template: str, categories=None) -> str:
    """生成世界书总结 system prompt（小说转世界书）。
    template：风格模板（detailed/concise/storyline）。
    categories：本次要提取的分类名列表；None 表示全部启用分类。
    """
    cats = categories or _ALL_CATEGORIES
    cats = [c for c in cats if c in WORLDBOOK_CATEGORY_TEMPLATES] or _ALL_CATEGORIES
    cat_lines = []
    for c in cats:
        fields = "、".join(f"{i+1}. {f}" for i, f in enumerate(WORLDBOOK_CATEGORY_TEMPLATES[c]))
        cat_lines.append(f"- {c}：{fields}")
    cat_block = "\n".join(cat_lines)
    plot_note = ""
    if "剧情" in cats:
        plot_note = """
【剧情条目特别说明】
- 除本章具体事件外，若本章对主线/支线有明显推进或埋下伏笔，可提取为「剧情」类条目（name 可用「主线剧情」「支线剧情」「关键转折点」「伏笔与暗线」），content 概括该线索在本章的发展与当前状态。
"""
    extra = {
        "detailed": "",
        "concise": "\n【简洁要求】content 每条尽量精简（3-6 行内），只保留最具区分度的设定信息，省略冗余修饰，但仍须满足分类字段与总分结构要求。",
        "storyline": "\n【剧情侧重】内容重点梳理剧情事件、人物关系变化与时间线推进；其他分类设定仍须满足字段要求，可适当从简。",
    }.get(template, "")

    return f"""你是一位小说设定分析师（Worldbook 世界书编辑），负责把整本小说逐步转化为可用的世界书（小说转世界书）：从章节中提取设定并整理为「世界书条目」。

世界书条目 = 一段可独立读取的设定，配有关键词（触发词）。后续 AI 修改小说内容时，只要正文中出现某条目的触发词，就会读取该条目作为参考。

任务：
1. 仔细阅读给定的章节内容
2. 按启用的分类提取所有关键设定，为每个设定生成一个条目
3. 输出结构化JSON

输出格式（严格JSON）：
{{
  "entries": [
    {{
      "category": "{'/'.join(cats)}",
      "name": "设定名称",
      "keys": ["触发词1", "触发词2"],
      "content": "设定完整描述（按对应分类字段模板 + 总分结构整理）",
      "first_appearance": "第X章"
    }}
  ],
  "summary": "本章一句话摘要"
}}

关键要求：
- category 必须是本次提取分类之一：{", ".join(cats)}
- 【合并规则】（角色特定身份/形态必须合并进角色本身条目，严禁单独建条）
  - 同一个实体（角色/地点/组织/物品/种族等）无论出现多少章、有多少形态/身份/别名，始终只输出【一个条目】，内容发展式追加，严禁拆分成多个条目
  - 角色的特定身份/形态/变身（如「康桥（天使形态）」「康桥-天使形态」「康桥·水蜥蜴形态」「林恩（恶魔状态）」等）一律并入该角色本身条目：name 只写角色主名（如「康桥」），该身份/形态的名称作为 keys 别名，其外貌/能力/背景经历按时期分条写入 content
  - 判定逻辑：名称中括号内、破折号/冒号/·/“的”之后的修饰部分，若含 形态/身份/状态/变身/时期/阶段/模样 等词，或修饰部分形如「XX形态/XX身份/XX状态」，则判定为该主实体的细分表现，必须并入主实体条目；若修饰部分是真实别名/称号（如「哥斯拉」是「康桥（哥斯拉）」的别称），则该别称作为 keys 而非单独条目
- 【触发词规则】keys 必须是从章节原文中实际出现过的真实名称、别名、称号、绰号（含特定身份/形态名称）；严禁使用分类字段名（如「称号与相关别称」「背景经历」「身份」「外貌」「技能」「重要事件」等字段模板名称）或 JSON 结构词（name/category/keys/content/entries/first_appearance）当作触发词；触发词必须是名词性的名称/称号（人名、地名、组织名、物品名等），严禁整句、动作描述、状态短语（如「耕农打猎为生」「撞毁车辆后进入虫洞」）
- 形态、能力、经历、事件等细节一律写入 content，作为设定的组成部分
- 只提取本章新出现或被明确重申的设定，不要重复已确定的旧条目
- 如果本章没有某类设定，entries 可以为空数组

【分类字段模板】（content 必须按对应分类的字段整理；未启用的分类不提取）：
{cat_block}
{plot_note}
【content 排版硬性要求】（对所有条目的 content 生效）：
- 必须严格按照"总分结构"分类分条列举设定、梳理各方面设定
- 使用 # 标题格式 区分各部分设定
- 用 1. 2. 3. 列举各部分设定
- 严禁使用 **粗体字** 格式（不要出现 **xxx**）
- 每部分必须先阐述概括性内容，再以"- 具体内容"的格式分条列举进一步细分的设定内容

【总结格式要求】（按怎样的格式方式总结、总结哪些内容）：
- 逐条通读给定内容，只提取【确实出现或被明确暗示】的设定，不臆造、不补全内容中不存在的信息
- 每个条目必须四要素齐全：category（分类）、name（主名）、keys（触发词）、content（完整设定）；first_appearance 标注该设定首次出现的章节号
- content 必须逐字段覆盖对应分类字段模板列出的所有内容：角色条目需含 名称/称号别称/性别/身份(含特定身份与形态)/外貌(分时期)/背景经历(按时间线分条，如 - 第X章：…)/性格/技能/重要事件/弱点/话语示例；其他分类同理
- 与已记录条目重合的旧信息不要重复输出，只输出本次新增、被重申或被更新的设定信息
- 背景经历一律按时间线分条叙述，标注章节出处；外貌存在时期变化或不同形态时，按时期/形态分别分条叙述{extra}"""


SUMMARIZER_SYSTEM = _build_summarizer_system("detailed")


CHAT_SYSTEM = """你是一位小说内容助手（Chat AI），能够回答读者对小说内容的任何问题。

可用信息：
- "相关记忆"是从全书中通过向量检索提取的相关段落，请基于这些内容如实回答
- "设定摘要"是从小说中自动梳理的角色、世界观、道具等结构化设定
- 如果答案不在记忆中，明确告诉用户"相关内容在记忆中未找到"，禁止编造
- 如果用户要求提取总结，请简洁、准确、有结构地回答
- 回答使用中文，条理清晰，必要时用列表展示
"""

WRITER_SYSTEM_FULL_CONTEXT = """你是一位资深网文编辑（Writer AI），负责根据用户的修改指令对小说段落进行精修、润色、扩写或改写。

在修改之前，你已获得以下完整上下文：
1. 当前章节全部内容
2. 小说的角色设定、世界观设定、剧情脉络
3. 从向量数据库检索的相关记忆段落
4. 用户的全局修改要求（global prompt）
5. 用户的自定义Agent设定

请充分利用这些信息，确保修改结果：
- 忠于原著设定，不OOC
- 与前后文逻辑自洽
- 语言自然流畅
- 对场景、对话、心理活动合理扩写

输出格式：
- 若用户给了多个段落，请保持段落一一对应，用 ===分段=== 分隔输出
- 每个分隔符前后都是一个完整的修改后段落
- 不要添加解释性文字、不要调整段落数量
"""


class TriModelAI:
    def __init__(self, novel_id: str):
        self.novel_id = novel_id
        self.ollama = OllamaClient()
        self.external = ExternalAPIClient()
        self.memory = ChromaMemory(novel_id)
        self._memory_ok = True
        cfg = load_config()["tri_ai"]
        self.writer_temp = cfg["writer_temperature"]
        self.reviewer_temp = cfg["reviewer_temperature"]
        self.chat_temp = cfg["chat_temperature"]
        self.top_k = cfg["retrieve_top_k"]
        self.allow_rename = bool(cfg.get("allow_ai_rename_chapter", False))  # 是否允许 AI 自动修改章节名
        self._settings_summary = None  # 缓存设定摘要
        self._worldbook = None         # 缓存世界书数据

    def _get_ai_client(self, provider=None, model=None, slot=None):
        """根据配置自动选择 AI 客户端：外部API优先，其次本地Ollama。
        provider/model/slot：可选的多模型协作覆盖（指定某功能使用哪个模型/槽位）。"""
        cfg = load_config().get("ai_provider", {})
        provider = provider or cfg.get("provider", "local")
        if provider == "external" and self.external.enabled:
            if slot:
                self.external.set_slot(slot)
            return self.external
        if model:
            self.ollama.set_model(model)
        return self.ollama

    # ========== 世界书（Worldbook）设定系统 ==========
    # 世界书条目 = 一段设定 + 触发关键词。AI 修改内容时按关键词命中读取对应条目，
    # 类似酒馆等 AI 聊天平台的 Lorebook 机制。用户可以自行编辑条目的触发词与内容。

    def _settings_path(self) -> Path:
        """旧版设定摘要文件（仅用于一次性迁移）"""
        return DATA_DIR / "novels" / self.novel_id / "settings_summary.json"

    def _worldbook_path(self) -> Path:
        return DATA_DIR / "novels" / self.novel_id / "worldbook.json"

    def load_worldbook(self) -> dict:
        """加载世界书数据；不存在时尝试从旧设定摘要迁移。"""
        if self._worldbook is not None:
            return self._worldbook
        wp = self._worldbook_path()
        if wp.exists():
            try:
                with open(wp, "r", encoding="utf-8") as f:
                    book = json.load(f)
                book.setdefault("entries", [])
                book.setdefault("last_chapter", 0)
                self._worldbook = book
                return book
            except Exception:
                pass
        book = self._migrate_worldbook()
        self._worldbook = book
        return book

    def save_worldbook(self, data: dict):
        self._worldbook = data
        wp = self._worldbook_path()
        wp.parent.mkdir(parents=True, exist_ok=True)
        with open(wp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _migrate_worldbook(self) -> dict:
        """把旧版 settings_summary.json 迁移为世界书条目（角色/种族/物品/世界观/关系/剧情）。"""
        book = {"entries": [], "last_chapter": 0}
        sp = self._settings_path()
        migrated = False
        if sp.exists():
            try:
                with open(sp, "r", encoding="utf-8") as f:
                    old = json.load(f)
                counter = [0]
                def add(cat, name, content, keys=None, extra=None):
                    if not name:
                        return
                    counter[0] += 1
                    e = {"id": f"wb_{counter[0]}", "category": cat, "name": name,
                         "keys": [k for k in (keys or []) if k] or [name],
                         "content": (content or "").strip(), "first_appearance": ""}
                    if extra:
                        e.update({k: v for k, v in extra.items() if v})
                    book["entries"].append(e)
                for c in old.get("characters", []):
                    parts = [f"性格：{'、'.join(c.get('traits', [])[:6])}。"] if c.get("traits") else []
                    if c.get("relations"):
                        parts.append(f"关系：{c.get('relations')}。")
                    if c.get("notes"):
                        parts.append(f"备注：{c.get('notes')}。")
                    add("角色", c.get("name", ""), f"{c.get('name', '')}。" + " ".join(parts),
                        [c.get("name", "")] + (c.get("aliases") or []),
                        {"first_appearance": c.get("first_appearance", "")})
                for w in old.get("worldbuilding", []):
                    cat = "种族" if str(w.get("category", "")).strip() == "种族" else "世界观"
                    add(cat, w.get("name", ""), (w.get("description") or "") + (f" 备注：{w.get('notes')}。" if w.get("notes") else ""),
                        [w.get("name", "")], {"first_appearance": w.get("first_mentioned", "")})
                for it in old.get("items", []):
                    content = (it.get("description") or "") + (f" 持有者：{it.get('owner')}。" if it.get("owner") else "")
                    add("物品", it.get("name", ""), content, [it.get("name", "")],
                        {"first_appearance": it.get("first_appearance", "")})
                for r in old.get("relationships", []):
                    add("关系", f"{r.get('from', '')}→{r.get('to', '')}", r.get("relation", ""),
                        [r.get("from", ""), r.get("to", "")], {"first_appearance": r.get("since", "")})
                for p in old.get("plot_points", []):
                    add("剧情", p.get("event", ""), p.get("significance", ""), [],
                        {"first_appearance": p.get("chapter", "")})
                book["last_chapter"] = old.get("last_chapter", 0)
                migrated = True
            except Exception:
                pass
        if migrated:
            self.save_worldbook(book)
        return book

    @staticmethod
    def _append_development(existing: str, new: str, max_len: int = 6000) -> str:
        """发展式追加设定内容：把新内容中旧内容尚未包含的行/段落追加到末尾，
        保留剧情演进与历史信息；同时保留换行结构。返回合并后的内容。"""
        new = (new or "").strip()
        if not new:
            return existing or ""
        existing = existing or ""
        if new in existing:
            return existing
        # 按换行切块（保留段落/行），只追加旧内容中没有的块
        blocks = re.split(r"\n+", new)
        to_add = []
        for b in blocks:
            b = b.strip()
            if b and b not in existing and b not in to_add:
                to_add.append(b)
        if not to_add:
            return existing
        added_block = "\n".join(to_add)
        combined = (existing.rstrip() + "\n\n" + added_block) if existing else added_block
        if len(combined) > max_len:
            combined = combined[:max_len] + "\n…（内容过长已截断）"
        return combined

    def _merge_worldbook_entries(self, book: dict, new_entries: list, chapter_idx: int) -> int:
        """把 AI 提取的新条目合并进世界书：
        - 同一实体（同 category 且规范名相同或触发词有交集）自动合并为一个条目，避免重复
        - 触发词取并集并清洗掉「细分表现」词；内容发展式追加（保留换行）
        返回新增条数。"""
        entries = book.setdefault("entries", [])
        added = 0
        for e in new_entries or []:
            cat = str(e.get("category", "")).strip() or "其他"
            raw_name = str(e.get("name") or "").strip()
            if not raw_name:
                continue
            norm = _norm_entry_name(raw_name) or raw_name
            new_keys = _clean_sub_keys(raw_name,
                                       [str(k).strip() for k in (e.get("keys") or []) if str(k).strip()])
            if not new_keys:
                new_keys = [norm]
            new_content = str(e.get("content") or "").strip()
            new_first = str(e.get("first_appearance") or "").strip()
            if not new_first and chapter_idx:
                new_first = f"第{chapter_idx}章"

            # 查找可合并目标：同 category 且 规范名相同 / 主名+“的”后缀 / 触发词有交集
            target = None
            new_key_set = set(new_keys)
            for old in entries:
                if old.get("category", "") != cat:
                    continue
                old_norm = _norm_entry_name(old.get("name", ""))
                if old_norm == norm:
                    target = old
                    break
                # “康桥” 与 “康桥的岩浆浴与高温成长” 视为同一实体（细分合并进主条目）
                if (norm.startswith(old_norm + "的") and len(old_norm) < len(norm)) or \
                   (old_norm.startswith(norm + "的") and len(norm) < len(old_norm)):
                    target = old
                    break
                # “康桥” 与 “康桥（天使形态）”“康桥-天使形态”“天使形态·康桥” 等形态细分名视为同一实体
                if (norm.startswith(old_norm) and len(norm) > len(old_norm) and
                        _has_form_marker(norm[len(old_norm):])) or \
                   (old_norm.startswith(norm) and len(old_norm) > len(norm) and
                        _has_form_marker(old_norm[len(norm):])):
                    target = old
                    break
                old_keys = set(old.get("keys") or [])
                if new_key_set and (old_keys & new_key_set):
                    target = old
                    break
            if target is None:
                entries.append({
                    "id": f"wb_{int(__import__('time').time() * 1000) % 100000}_{len(entries) + added}",
                    "category": cat, "name": norm,
                    "keys": new_keys,
                    "content": new_content,
                    "first_appearance": new_first
                })
                added += 1
            else:
                # 名称保留更基础（更短）的规范名，如 康桥（哥斯拉）→ 康桥
                if len(norm) < len(target.get("name", "")):
                    target["name"] = norm
                # 触发词并集（已清洗细分词）
                old_keys = set(target.get("keys") or [])
                target["keys"] = list(old_keys) + [k for k in new_keys if k not in old_keys]
                # 内容发展式追加：累计新增信息，保留换行
                target["content"] = self._append_development(target.get("content", ""),
                                                             new_content)
                if not target.get("first_appearance"):
                    target["first_appearance"] = new_first
        return added

    @staticmethod
    def _match_worldbook(entries: list, text: str, limit: int = 8) -> list:
        """关键词触发：返回内容中出现触发词的条目（最多 limit 条）。无触发词的条目不自动触发。"""
        if not entries or not text:
            return []
        tl = text.lower()
        hit = []
        for e in entries:
            keys = e.get("keys") or []
            if not keys:
                continue
            for k in keys:
                kk = str(k).strip().lower()
                if kk and kk in tl:
                    hit.append(e)
                    break
            if len(hit) >= limit:
                break
        return hit

    def load_settings_summary(self) -> dict:
        """兼容旧接口：从世界书构建旧结构设定摘要（供 reviewer_check / chat_answer 等继续使用）。"""
        book = self.load_worldbook()
        chars, world, items, rels, plots = [], [], [], [], []
        for e in book.get("entries", []):
            cat = e.get("category", "")
            keys = e.get("keys") or []
            if cat == "角色":
                chars.append({
                    "name": e.get("name", ""),
                    "traits": e.get("traits") or [],
                    "aliases": keys[1:] if keys else [],
                    "relations": "",
                    "notes": e.get("notes", ""),
                    "first_appearance": e.get("first_appearance", "")
                })
            elif cat in ("世界观", "种族", "地点", "组织", "知识书"):
                world.append({
                    "name": e.get("name", ""),
                    "category": "种族" if cat == "种族" else ("地点" if cat == "地点" else ("组织" if cat == "组织" else "世界观")),
                    "description": e.get("content", ""),
                    "first_mentioned": e.get("first_appearance", ""),
                    "notes": e.get("notes", "")
                })
            elif cat == "物品":
                items.append({
                    "name": e.get("name", ""), "type": "", "owner": "",
                    "description": e.get("content", ""),
                    "first_appearance": e.get("first_appearance", "")
                })
            elif cat == "关系":
                frm, _, to = e.get("name", "").partition("→")
                rels.append({"from": frm, "to": to, "relation": e.get("content", ""),
                             "nature": "", "since": e.get("first_appearance", "")})
            elif cat == "剧情":
                plots.append({"event": e.get("name", ""), "significance": e.get("content", ""),
                              "chapter": e.get("first_appearance", "")})
        return {"characters": chars, "worldbuilding": world, "items": items,
                "plot_points": plots, "relationships": rels, "summary": "",
                "last_chapter": book.get("last_chapter", 0)}

    def reviewer_summarize_chapter(self, chapter_idx: int, chapter_title: str,
                                    chapter_content: str, template: str = "detailed",
                                    categories=None, max_chars: Optional[int] = 5000,
                                    model_override: Optional[dict] = None) -> dict:
        """
        审校 Agent 自动阅读章节，提取角色/种族/物品等设定为世界书条目（带触发词）。
        自动去重合并到 worldbook.json。
        template：世界书总结风格模板（detailed/concise/storyline）。
        categories：本次要提取的分类名列表（None=全部），见 WORLDBOOK_CATEGORY_TEMPLATES。
        max_chars：章节内容输入上限；传 None 表示不截断（阅读模式按 ≥10000 汉字整批输入）。
        model_override：多模型协作，指定本次总结使用的模型，如 {"provider":"local","model":"qwen2.5:7b"} 或 {"provider":"external","slot":"default"}。
        """
        client = self._get_ai_client(**(model_override or {}))

        book = self.load_worldbook()
        existing = [f"[{e.get('category', '')}] {e.get('name', '')}" for e in book.get("entries", [])]
        existing_block = "\n".join(existing[:100]) or "（暂无）"
        content_part = chapter_content[:max_chars] if max_chars else chapter_content

        prompt = f"""当前章节：第{chapter_idx}章《{chapter_title}》

【已记录的世界书条目（避免重复提取，仅列前100条）】
{existing_block}

【合并要求】若本次要输出的实体与已记录条目为同一实体（含角色特定身份/形态，如「康桥（天使形态）」「康桥-天使形态」属于「康桥」），必须合并进已有条目：name 用已记录的主名，严禁新建条目；只把新增信息追加进 content，把新别名/称号加入 keys。

【章节内容】
{content_part}

请严格按JSON格式输出新出现或被重申的新设定条目。只提取新的，不重复已有条目。"""

        FORMAT_REMIND = _WORLDBOOK_FORMAT_REMIND

        # 最多尝试 3 次：兼容本地模型输出不稳定（格式错误 / Ollama 500）
        for attempt in range(3):
            use_prompt = prompt + (FORMAT_REMIND if attempt else "")
            res = client.generate(prompt=use_prompt, system=_build_summarizer_system(template, categories),
                                  temperature=0.3, stream=False)
            content = (res or {}).get("content", "")
            if res and res.get("error"):
                if attempt < 2:
                    time.sleep(6)
                    continue
                return {"error": res["error"], "chapter": chapter_idx}

            entries = _extract_worldbook_entries(content)
            if entries is None:
                if attempt < 2:
                    time.sleep(4)
                    continue
                return {"error": "JSON解析失败: 输出格式不符合要求", "raw": content[:200],
                        "chapter": chapter_idx}

            # 严格清洗触发词：只保留原文出现的真实名称，剔除字段名误提取
            entries = _sanitize_trigger_keys(entries, content_part)

            # 解析成功（含空数组）：去重合并进世界书
            try:
                added = self._merge_worldbook_entries(book, entries, chapter_idx)
            except Exception as e:
                return {"error": f"合并世界书失败: {e}", "raw": content[:200], "chapter": chapter_idx}
            book["last_chapter"] = chapter_idx
            self.save_worldbook(book)
            return {"ok": True, "chapter": chapter_idx, "added": added,
                    "entries": len(entries), "total": len(book.get("entries", []))}

    def summarize_worldbook_keyword(self, keyword: str, context_text: str,
                                    template: str = "detailed",
                                    model_override: Optional[dict] = None) -> dict:
        """
        针对特定关键词/实体的定向设定总结：
        AI 通读与该关键词相关的章节片段，总结该实体的设定并输出世界书条目（1-2 条）。
        返回 {"entries": [...]}；失败返回 {"error": ...}
        """
        client = self._get_ai_client(**(model_override or {}))
        prompt = f"""【目标实体 / 关键词】{keyword}

【检索到的相关章节片段（按章节顺序）】
{context_text}

请基于以上内容，总结「{keyword}」这一实体的完整设定，输出 1 个（最多 2 个）世界书条目。
要求：
- 若内容涉及多个不同实体，只输出与「{keyword}」直接相关的条目
- category 自动判断：角色/种族/物品/世界观/剧情/关系
- keys 必须包含该实体的所有名称、别名、称号
- content 严格按角色结构与总分结构要求整理
- 只输出JSON，禁止其他内容"""

        for attempt in range(3):
            use_prompt = prompt + (_WORLDBOOK_FORMAT_REMIND if attempt else "")
            res = client.generate(prompt=use_prompt, system=_build_summarizer_system(template),
                                  temperature=0.3, stream=False)
            content = (res or {}).get("content", "")
            if res and res.get("error"):
                if attempt < 2:
                    time.sleep(6)
                    continue
                return {"error": res["error"]}
            entries = _extract_worldbook_entries(content)
            if entries is None:
                if attempt < 2:
                    time.sleep(4)
                    continue
                return {"error": "JSON解析失败: 输出格式不符合要求", "raw": content[:200]}
            entries = _sanitize_trigger_keys(entries, context_text)
            return {"entries": entries, "raw_len": len(content)}

    # ========== 统一上下文构建 ==========

    def _build_full_context(self, current_text: str, instruction: str,
                            chapter_idx: Optional[int] = None,
                            chapter_title: str = "",
                            global_prompt: str = "",
                            custom_instruction: str = "",
                            style_instruction: str = "",
                            match_text: str = "") -> str:
        """
        构建完整上下文Prompt块，供Writer/Reviewer使用。
        包含：当前章节信息 + 世界书触发设定 + 向量记忆 + 全局要求 + 自定义Agent + 文风要求
        match_text：用于关键词触发世界书的完整章节文本（默认回退到 current_text）。
                   避免关键词只出现在章节中后段时漏读相关世界书设定。
        """
        parts = []

        # 0. 当前章节信息（让 AI 明确知道正在修改哪一章、章节名叫什么）
        if chapter_idx is not None:
            if chapter_title:
                parts.append(f"【当前修改章节】第{chapter_idx}章《{chapter_title}》")
            else:
                parts.append(f"【当前修改章节】第{chapter_idx}章")

        # 1. 世界书：关键词触发读取（正文中出现触发词才注入对应设定）
        book = self.load_worldbook()
        matched = self._match_worldbook(book.get("entries", []),
                                        (match_text or current_text or "") + " " + (instruction or ""))
        if matched:
            lines = ["📖 【世界书设定·已命中】"]
            for e in matched:
                keys = "、".join(e.get("keys", []) or [])
                lines.append(f"▸ [{e.get('category', '其他')}] {e.get('name', '')}（触发词：{keys}）")
                lines.append(f"   {e.get('content', '')[:180]}")
            parts.append("\n".join(lines))

        # 2. 向量记忆
        if chapter_idx is not None:
            refs = self._safe_retrieve(
                query=(current_text or "")[:800] + " " + (instruction or ""),
                chapter_idx=chapter_idx, exclude_chapter=True
            )
            if refs:
                mem_lines = ["📚 【相关记忆段落】"]
                for r in refs:
                    meta = r.get("meta", {})
                    mem_lines.append(
                        f"[第{meta.get('chapter_idx','?')}章《{meta.get('chapter_title','')}》|"
                        f"相关度{r.get('score','?')}]\n{r.get('content','')[:180]}"
                    )
                parts.append("\n\n".join(mem_lines))

        # 3. 全局要求 + 自定义Agent + 文风要求
        if global_prompt:
            parts.insert(0, f"🌐 【全局修改要求】\n{global_prompt}")
        if custom_instruction:
            parts.insert(0, f"🎭 【自定义Agent设定】\n{custom_instruction}")
        if style_instruction:
            parts.insert(0, f"✍️ 【文风要求】\n{style_instruction}")

        return "\n\n===\n\n".join(parts) if parts else ""

    # ========== 章节改名控制 ==========

    def _rename_instruction(self) -> str:
        """根据开关生成章节改名相关的 prompt 指示。"""
        if self.allow_rename:
            return ("\n\n【章节名称】若你认为当前章节名未能准确概括本章内容，允许在输出文本的"
                    "最末尾单独附加一行【新章节名】xxx 来给出建议的新标题；若无需改名则不要输出该标记。"
                    "该行不计入正文，切勿写进正文中间。")
        return "\n\n【章节名称】严禁修改章节标题，禁止在输出中出现【新章节名】标记。"

    @staticmethod
    def _extract_rename_tag(text: str) -> Tuple[str, Optional[str]]:
        """从 AI 输出中提取【新章节名】标记，返回 (去掉标记后的文本, 新标题或None)。"""
        m = _RENAME_TAG_RE.search(text)
        if not m:
            return text, None
        title = m.group(1).strip()
        cleaned = (text[: m.start()] + text[m.end():]).strip()
        return cleaned, (title or None)

    # ========== Writer：改写/润色（统一上下文版） ==========

    def writer_rewrite_unified(self, paragraphs: list, instruction: str,
                               chapter_idx: Optional[int] = None,
                               chapter_title: str = "",
                               global_prompt: str = "",
                               custom_instruction: str = "",
                               style_instruction: str = ""):
        """
        将所有段落合并为一次 AI 调用，同时附上完整上下文（章节信息+设定摘要+向量记忆+全局要求）。
        成功返回 (段落列表, 新章节名或None)；失败返回 {"error": ...}
        """
        if not paragraphs:
            return [], None

        SEP = "\n===分段===\n"
        combined = SEP.join(paragraphs)
        para_count = len(paragraphs)

        full_context = self._build_full_context(
            current_text=combined, instruction=instruction,
            chapter_idx=chapter_idx, chapter_title=chapter_title,
            global_prompt=global_prompt,
            custom_instruction=custom_instruction,
            style_instruction=style_instruction
        )

        user_prompt = f"""{full_context}

【修改指令】
{instruction}

【待修改文本（共{para_count}段，以 ===分段=== 分隔）】
{combined}

重要输出要求：
1. 你需要一次阅读以上所有段落和上下文，通盘理解后再整体改写，确保前后一致、没有OOC。
2. 改写后可以用 ===分段=== 分隔输出。你可以根据需要适当拆分过长段落、合并过短段落、或新增过渡段落，让内容更自然流畅。
3. 输出段落数量不做严格限制，以内容质量为优先。但不要添加解释性文字。
4. 如果某段不需要改，请保持原段落输出。禁止跳过、省略任何段落。{self._rename_instruction()}"""

        client = self._get_ai_client()
        res = client.generate(
            prompt=user_prompt,
            system=WRITER_SYSTEM_FULL_CONTEXT,
            temperature=self.writer_temp,
            stream=False
        )
        content = (res or {}).get("content", "")
        if res and res.get("error"):
            return {"error": res["error"]}

        # 无论开关如何都移除标记，避免标记残留进正文；开关决定是否采用新标题
        content, new_title = self._extract_rename_tag(content)
        if not self.allow_rename:
            new_title = None

        parts = [p.strip() for p in content.split("===分段===")]
        # 过滤掉完全空白的段（保留至少包含非空字符的段）
        parts = [p for p in parts if p]
        # 如果AI输出为空，返回原段落作为兜底
        if not parts:
            return [p.strip() for p in paragraphs], new_title

        return parts, new_title

    def writer_rewrite(self, original_text: str, instruction: str,
                       chapter_idx: Optional[int] = None,
                       chapter_title: str = "",
                       global_prompt: str = "",
                       custom_instruction: str = "",
                       style_instruction: str = "",
                       stream: bool = False):
        """单段改写（带完整上下文，包含章节序号与名字）"""
        full_context = self._build_full_context(
            current_text=original_text, instruction=instruction,
            chapter_idx=chapter_idx, chapter_title=chapter_title,
            global_prompt=global_prompt,
            custom_instruction=custom_instruction,
            style_instruction=style_instruction
        )

        user_prompt = f"""{full_context}

【当前修改指令】
{instruction}

【原文】
{original_text}

请按修改指令对原文进行改写，直接输出修改后的完整文本。章节标题不在本任务范围内，禁止输出【新章节名】标记。"""

        client = self._get_ai_client()
        return client.generate(
            prompt=user_prompt,
            system=WRITER_SYSTEM_FULL_CONTEXT,
            temperature=self.writer_temp,
            stream=stream
        )

    def plan_chapter_modification(self, chapter_idx: int, chapter_title: str,
                                  chapter_content: str, keywords: Optional[list] = None,
                                  instruction: str = "", global_prompt: str = "",
                                  custom_instruction: str = "",
                                  style_instruction: str = "",
                                  neighbor_context: str = "",
                                  model_override: Optional[dict] = None) -> dict:
        """批量修改前的预思考：审校 Agent 先阅读本章、前后章节上下文与命中的世界书设定，
        输出本章应修改哪些内容、如何修改。
        返回 {"plan": [...], "focus": "..."}；失败返回 {"error": ...}"""
        # 附上按关键词命中的世界书设定，确保规划基于正确设定
        book = self.load_worldbook()
        matched = self._match_worldbook(book.get("entries", []),
                                        (chapter_content or "") + " " + (instruction or ""))
        if matched:
            setting_lines = ["已命中设定："]
            for e in matched:
                keys = "、".join(e.get("keys", []) or [])
                setting_lines.append(f"- [{e.get('category', '其他')}] {e.get('name', '')}（触发词：{keys}）")
                setting_lines.append(f"  {e.get('content', '')[:150]}")
            settings_block = "\n".join(setting_lines)
        else:
            settings_block = "（无命中设定）"
        kw_list = "、".join(keywords or [])

        neighbor_block = ""
        if neighbor_context:
            neighbor_block = f"【前后章节上下文（用于保证设定衔接一致，避免疏漏）】\n{neighbor_context}\n"

        prompt = f"""【当前章节】第{chapter_idx}章《{chapter_title}》
{neighbor_block}【命中关键词】{kw_list or "（无，全章节模式由AI自行判断）"}
【修改要求】{instruction}
{("【文风要求】" + style_instruction) if style_instruction else ""}
【已命中设定】
{settings_block}
【章节全文】
{chapter_content[:6000]}

请先通读本章与已整理设定，判断本章是否需要修改，并预思考需要修改哪些内容、如何修改，以保证修改后的内容符合全书设定。严格输出JSON：
{{"need_modify": true, "plan":[{{"target":"要修改的对象或位置","change":"具体如何修改"}}],"focus":"本章修改重点（一句话）"}}

要求：
- 严格判断：只有当本章确实存在与修改要求/已整理设定相关的、需要调整的内容时才输出 need_modify=true；若本章内容已符合要求或与修改主题无关，必须输出 need_modify=false 且 plan 为空，避免无意义的修改消耗
- plan 只列真正需要修改的 1-5 个点；无法确定本章是否要改时取 need_modify=false（宁可不改也不误改，避免浪费 token 与破坏原文）
- 修改必须符合已整理设定，不OOC
- 只输出JSON，禁止其他内容"""

        client = self._get_ai_client(**(model_override or {}))
        res = client.generate(prompt=prompt,
                              system="你是一位严谨的小说设定编辑，负责判断章节是否需要修改并制定修改方案。",
                              temperature=0.3, stream=False)
        content = (res or {}).get("content", "")
        if res and res.get("error"):
            return {"error": res["error"]}
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(content[start:end])
                data.setdefault("plan", [])
                data.setdefault("focus", "")
                # 缺少 need_modify 字段时按 plan 内容保守判断：plan 为空即视为无需修改（默认跳过，避免误改）
                data.setdefault("need_modify", bool(data.get("plan")))
                return data
        except (json.JSONDecodeError, KeyError) as e:
            return {"error": f"JSON解析失败: {e}", "raw": content[:150]}
        return {"error": "未找到有效JSON", "raw": content[:150]}

    def bulk_modify_chapter(self, chapter_idx: int, chapter_title: str,
                            chapter_content: str, keyword_contexts: Optional[list] = None,
                            keywords: Optional[list] = None, instruction: str = "",
                            global_prompt: str = "", custom_instruction: str = "",
                            style_instruction: str = "",
                            already_modified_context: str = "",
                            neighbor_context: str = "",
                            modification_plan: Optional[dict] = None,
                            model_override: Optional[dict] = None) -> object:
        """总体设定修改：根据关键词命中的上下文，按修改要求修改整个章节。
        already_modified_context：前面已修改章节的回顾，用于保持跨章节一致、避免重复冗杂。
        neighbor_context：前后章节上下文（用于保证相邻章节衔接自然）。
        modification_plan：预思考的修改方案（plan_chapter_modification 输出），用于保证设定正确性。
        返回修改后的完整章节文本(str)；失败返回 {"error": ...}"""
        full_context = self._build_full_context(
            current_text=(chapter_content or "")[:800],
            instruction=instruction,
            chapter_idx=chapter_idx,
            global_prompt=global_prompt,
            custom_instruction=custom_instruction,
            style_instruction=style_instruction,
            match_text=chapter_content  # 用整章内容匹配世界书触发词，避免中后段关键词漏读
        )
        kw_text = "\n".join(
            f"[命中关键词「{s.get('keyword', '')}」位置]\n{s.get('context', '')}"
            for s in (keyword_contexts or [])
        ) or "（无命中上下文）"
        kw_list = "、".join(keywords or [])

        already_block = ""
        if already_modified_context:
            already_block = f"""

【已修改章节回顾（用于保持跨章节一致性）】
{already_modified_context}

注意：以上章节已按你的要求完成修改。请参考它们，保持设定、措辞、叙事风格完全一致，并避免与它们的修改重复冗杂（例如：不要重复交代同一设定、不要重复插入相同性质的描写）。"""

        neighbor_block = ""
        if neighbor_context:
            neighbor_block = f"""

【前后章节上下文（用于保证相邻章节衔接自然、不产生设定疏漏）】
{neighbor_context}

注意：修改本章时请确保与前一章结尾、后一章开头自然衔接，不重复、不冲突。"""

        plan_block = ""
        if modification_plan:
            plan_items = modification_plan.get("plan") or []
            if plan_items:
                lines = [f"- 修改对象：{p.get('target','')}；修改方式：{p.get('change','')}" for p in plan_items]
                plan_block = f"""

【本章预思考修改方案（请据此执行）】
{chr(10).join(lines)}
修改重点：{modification_plan.get('focus','')}"""
            elif modification_plan.get("focus"):
                plan_block = f"""

【本章预思考修改重点】
{modification_plan.get('focus','')}"""

        user_prompt = f"""{full_context}{already_block}{neighbor_block}{plan_block}

【本次修改章节】第{chapter_idx}章《{chapter_title}》
【检索关键词】{kw_list}
【关键词出现的上下文】
{kw_text}

【修改要求】
{instruction}

【章节全文】
{chapter_content}

请基于检索到的上下文与修改要求，对本章进行相应修改。硬性要求：
1. 只修改需要变化的内容，其余内容保持原样
2. 保持原章节的段落结构与叙事风格
3. 直接输出修改后的完整章节全文，禁止JSON包裹、禁止添加任何解释文字
4. 修改必须忠实于既有设定，避免OOC（角色崩坏）和设定冲突
5. 若「已修改章节回顾」非空，必须与前面章节已修改的设定、措辞保持一致，不得重复冗杂
6. 若提供了「前后章节上下文」，必须与前一章结尾、后一章开头自然衔接，不重复、不冲突
7. 若提供了「预思考修改方案」，严格按方案执行修改点{self._rename_instruction()}"""

        client = self._get_ai_client(**(model_override or {}))
        res = client.generate(prompt=user_prompt, system=WRITER_SYSTEM_FULL_CONTEXT,
                              temperature=self.writer_temp, stream=False)
        if res and res.get("error"):
            return {"error": res["error"]}
        content = (res or {}).get("content", "")
        if not (content or "").strip():
            return {"error": "AI 返回内容为空"}
        # 无论开关如何都移除标记，避免标记残留进正文；开关决定是否采用新标题
        content, new_title = self._extract_rename_tag(content)
        if not (content or "").strip():
            return {"error": "AI 返回内容为空"}
        if not self.allow_rename:
            new_title = None
        return content, new_title

    # ---------- Reviewer：一致性审校 ----------
    def reviewer_check(self, text: str, chapter_idx: Optional[int] = None):
        settings = self.load_settings_summary()
        settings_block = ""
        if settings and settings.get("characters"):
            lines = ["【已记录角色设定】"]
            for c in settings.get("characters", [])[:10]:
                lines.append(f"{c.get('name','?')}：{'、'.join(c.get('traits',[])[:3])}")
            settings_block = "\n".join(lines)

        context_parts = [settings_block] if settings_block else []
        if chapter_idx is not None:
            refs = self._safe_retrieve(
                query=text[:800], chapter_idx=chapter_idx,
                exclude_chapter=True, max_items=4
            )
            for r in refs:
                meta = r["meta"]
                context_parts.append(f"[第{meta.get('chapter_idx','?')}章《{meta.get('chapter_title','')}》]\n{r['content']}")

        context_block = "\n\n".join([p for p in context_parts if p]) or "（无前文参考）"

        user_prompt = f"""【前文设定参考】
{context_block}

【待审校文本】
{text}

请严格按输出格式返回JSON。"""

        client = self._get_ai_client()
        res = client.generate(prompt=user_prompt, system=REVIEWER_SYSTEM, temperature=self.reviewer_temp)
        content = (res or {}).get("content", "")
        if res and res.get("error"):
            return {"error": res["error"]}
        try:
            start = content.find("[")
            start_obj = content.find("{")
            if start != -1 and (start_obj == -1 or start < start_obj):
                end = content.rfind("]") + 1
                return {"issues": __import__("json").loads(content[start:end])}
            if start_obj != -1:
                end_obj = content.rfind("}") + 1
                return __import__("json").loads(content[start_obj:end_obj])
        except Exception:
            pass
        return {"raw": content, "parse_failed": True}

    # ---------- Chat：小说内容问答 ----------
    def chat_answer(self, question: str, history: Optional[List[Dict]] = None, stream: bool = False):
        refs = self._safe_retrieve(query=question, max_items=self.top_k)
        context_parts = []
        for r in refs:
            meta = r["meta"]
            ctitle = meta.get("chapter_title", "")
            cidx = meta.get("chapter_idx", "?")
            context_parts.append(f"[第{cidx}章《{ctitle}》|相关度{r['score']}]\n{r['content']}")

        # 附加设定摘要
        settings = self.load_settings_summary()
        if settings.get("summary"):
            context_parts.insert(0, f"[全书摘要]\n{settings['summary']}")
        if settings.get("characters"):
            chars_brief = "; ".join([f"{c['name']}:{'、'.join(c.get('traits',[])[:2])}"
                                      for c in settings.get("characters", [])[:8]])
            context_parts.insert(0, f"[角色]\n{chars_brief}")

        context_block = "\n\n".join(context_parts) if context_parts else "（记忆中未检索到相关段落）"

        user_prompt = f"""【用户问题】
{question}

【相关记忆与设定】
{context_block}

请基于相关记忆准确回答。"""

        messages = [{"role": "system", "content": CHAT_SYSTEM}]
        if history:
            for m in history[-6:]:
                messages.append({"role": m.get("role", "user"), "content": m.get("content", "")})
        messages.append({"role": "user", "content": user_prompt})

        client = self._get_ai_client()
        return client.chat(messages, temperature=self.chat_temp, stream=stream)

    def get_memory_refs(self, query: str, top_k: int = 5, chapter_idx: int = None) -> List[Dict]:
        return self._safe_retrieve(query=query, chapter_idx=chapter_idx,
                                   exclude_chapter=bool(chapter_idx), max_items=top_k)

    def _safe_retrieve(self, query: str, chapter_idx: Optional[int] = None,
                       exclude_chapter: bool = False, max_items: int = 5) -> List[Dict]:
        if not self._memory_ok:
            return []
        try:
            refs = self.memory.retrieve(
                query=query, top_k=self.top_k,
                chapter_idx=chapter_idx, exclude_chapter=exclude_chapter
            )
            return refs[:max_items] if refs else []
        except Exception:
            self._memory_ok = False
            return []


# ========== 设定合并工具函数 ==========

# AI 输出中新章节名的标记格式（见 _rename_instruction）
_RENAME_TAG_RE = re.compile(r"【新章节名】\s*([^\n【】]+)")


# 形态细分标记词：名称中括号内 / 分隔符后的修饰部分含这些词时，视为主实体的细分表现（应并入主条目）
_FORM_MARKERS = ("形态", "状态", "身份", "时期", "阶段", "变身", "模样")


def _has_form_marker(s: str) -> bool:
    """判断字符串是否为形态/身份等细分修饰（如「天使形态」「恶魔状态」）。"""
    return any(mk in s for mk in _FORM_MARKERS)


_WORLDBOOK_FORMAT_REMIND = (
    "\n\n【重要】必须严格输出一个JSON对象：{\"entries\":[{\"category\":...,\"name\":...,"
    "\"keys\":[...],\"content\":...,\"first_appearance\":...}]}，entries 必须是数组；"
    "严禁输出数组顶层、编号对象（如 {\"1\":{...}}）、代码块或任何JSON以外的文字。"
)


def _extract_json_value(text: str):
    """容错提取文本中第一个完整的 JSON 对象/数组（支持括号平衡与字符串转义）。
    返回解析后的值；无法提取返回 None。"""
    if not text:
        return None
    stack = []
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            if start < 0:
                start = i
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                continue
            stack.pop()
            if not stack and start >= 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _extract_worldbook_entries(content: str):
    """解析世界书总结的 AI 输出 → 返回条目列表。
    支持：markdown 代码块、顶层数组 [ {...} ]、{"entries":[...]}、
          {"1":{...},"2":{...}} 编号对象；格式不合法返回 None。"""
    if not content:
        return None
    text = content.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    value = _extract_json_value(text)
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("entries"), list):
            return value["entries"]
        vals = list(value.values())
        if vals and all(isinstance(v, dict) and ("name" in v or "keys" in v or "content" in v)
                        for v in vals):
            return vals
    return None


def _norm_entry_name(name: str) -> str:
    """规范化条目名称：去掉括号中的细分表现（形态/能力等），如「康桥（哥斯拉）」→「康桥」；
    并去掉破折号/冒号/· 等分隔符后的形态细分，如「康桥-天使形态」→「康桥」。"""
    n = re.sub(r"[（(][^）)]*[）)]", "", name or "").strip()
    m = re.match(r"^(.*?)[—–\-·:：]+(.+)$", n)
    if m:
        main, tail = m.group(1).strip(), m.group(2).strip()
        if any(mk in tail for mk in _FORM_MARKERS):
            n = main
    return n


# 触发词黑名单：分类模板字段名 / JSON 结构词等（AI 易把这些词误当作触发词提取，无区分度）
_FIELD_WORD_BLACKLIST = {
    "名称", "称号", "称号与相关别称", "称号与相关词", "别称", "性别", "MBTI", "年龄",
    "身份", "外貌", "背景", "背景经历", "经历", "性格", "技能", "重要事件", "弱点",
    "话语示例", "话语", "示例", "位置", "特征", "性质", "成员", "目标", "功能",
    "来源", "持有者", "能力", "栖息地", "与人类关系", "关系", "世界规则", "力量体系",
    "历史背景", "历史", "地理", "主线剧情", "支线剧情", "关键转折点", "转折点",
    "伏笔与暗线", "伏笔", "暗线", "双方", "关系性质", "发展过程", "发展", "条目定义",
    "条目定义/说明", "定义", "说明", "相关细节", "细节", "影响与用途", "影响", "用途",
    "name", "category", "keys", "content", "entries", "first_appearance", "summary",
    "设定", "分类", "关键词", "触发词", "世界书", "角色", "地点", "组织", "物品",
    "种族", "世界观", "剧情", "知识书", "其他", "小说", "章节", "内容", "简介",
    "标题", "主线", "支线", "相关词", "正文",
    "世界观设定", "人物设定", "角色设定", "世界设定", "故事背景", "背景设定",
    "人物介绍", "角色介绍", "设定介绍", "身份设定", "外貌特征", "基本信息",
    "内容简介", "章节标题", "第一人称", "第三人称",
}


def _sanitize_trigger_keys(entries, source_text=None):
    """清洗 AI 输出的世界书条目触发词：
    1. 剔除字段名/JSON结构词等误提取词（_FIELD_WORD_BLACKLIST）
    2. 剔除单字词（区分度低、易误触发）
    3. 若给出来源原文，只保留在原文中实际出现过的触发词（严格从原文提取）
    返回清洗后的 entries。"""
    for e in entries or []:
        keys = []
        for k in (e.get("keys") or []):
            kk = str(k).strip()
            if len(kk) < 2:
                continue
            if kk in _FIELD_WORD_BLACKLIST:
                continue
            if source_text and kk not in source_text:
                continue
            keys.append(kk)
        # 全部被清洗掉时，兜底用主名作为唯一触发词，保证条目仍可触发
        if not keys:
            main = _norm_entry_name(e.get("name") or "")
            if main and len(main) >= 2:
                keys = [main]
        e["keys"] = keys  # 无条件覆盖
    return entries


def _clean_sub_keys(name: str, keys: list) -> list:
    """清洗触发词：剔除与条目名中细分表现相关的词（形态类括号内容、主名后的“的XX”后缀）。
    这类「细分表现」命名不应作为触发词，而应属于角色设定内容。
    注意：括号内若是真实别名（如 康桥（哥斯拉）→ 哥斯拉），则保留。"""
    if not keys:
        return keys
    # 细分表现标记词：括号内容含这些词时才视为形态/能力细分，需要剔除其相关触发词
    SUB_MARKERS = ("形态", "状态", "能力", "表现", "境界", "身份", "模样", "时期", "阶段")
    # 常见的形态/俗称类词（细分表现，不应作触发词；如 四脚蛇、蜥蜴）
    SUB_BLACKLIST = ("水蜥蜴", "蜥蜴", "四脚蛇", "爬虫", "巨蜥", "壁虎", "甲壳")
    drop_brackets = [b for b in re.findall(r"[（(]([^）)]*)[）)]", name or "")
                     if b and any(mk in b for mk in SUB_MARKERS)]
    # 主名后的“的XX”后缀细分词：康桥的岩浆浴与高温成长 → 岩浆浴与高温成长
    m = re.search(r"的(.+)$", name or "")
    suffix = m.group(1) if m else ""
    cleaned = []
    for k in keys:
        kk = str(k).strip()
        if not kk:
            continue
        drop = False
        for b in drop_brackets:
            if kk in b or b in kk:
                drop = True
                break
        if not drop and suffix and (kk in suffix or suffix in kk):
            drop = True
        if not drop and kk in SUB_BLACKLIST:
            drop = True
        if not drop:
            cleaned.append(kk)
    return cleaned

