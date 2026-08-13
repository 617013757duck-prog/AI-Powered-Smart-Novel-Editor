from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from config.settings import PROMPTS_FILE


class PromptService:
    """Prompt 仓库服务：支持保存、分类、搜索和一键应用自定义 prompt 模板。"""

    CATEGORIES = ["instruction", "global", "agent"]  # 修改要求 / 全局指令 / Agent设定

    def __init__(self):
        self.path: Path = PROMPTS_FILE
        self._ensure_file()

    def _ensure_file(self):
        if not self.path.exists():
            self._write([])

    def _read(self) -> List[Dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _write(self, data: List[Dict]):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ========== CRUD ==========
    def list_prompts(self, category: Optional[str] = None,
                     keyword: Optional[str] = None,
                     tag: Optional[str] = None) -> List[Dict]:
        prompts = self._read()
        if category:
            prompts = [p for p in prompts if p.get("category") == category]
        if tag:
            prompts = [p for p in prompts if tag in (p.get("tags") or [])]
        if keyword:
            kw = keyword.lower()
            prompts = [p for p in prompts if
                       kw in (p.get("title") or "").lower()
                       or kw in (p.get("content") or "").lower()
                       or any(kw in t.lower() for t in (p.get("tags") or []))]
        # 按更新时间倒序
        prompts.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
        return prompts

    def all_tags(self) -> List[str]:
        tags = set()
        for p in self._read():
            for t in (p.get("tags") or []):
                if t:
                    tags.add(t)
        return sorted(tags)

    def get_prompt(self, pid: str) -> Optional[Dict]:
        for p in self._read():
            if p.get("id") == pid:
                return p
        return None

    def save_prompt(self, data: Dict) -> Dict:
        prompts = self._read()
        now = int(time.time())
        pid = data.get("id")
        if pid:
            # 更新
            for i, p in enumerate(prompts):
                if p.get("id") == pid:
                    merged = {**p, **data}
                    merged["updated_at"] = now
                    merged.setdefault("created_at", p.get("created_at", now))
                    merged["tags"] = [t.strip() for t in (merged.get("tags") or []) if t and t.strip()]
                    prompts[i] = merged
                    self._write(prompts)
                    return merged
            # id 不存在则走新增
        # 新增
        pid = data.get("id") or uuid.uuid4().hex[:10]
        new_p = {
            "id": pid,
            "title": (data.get("title") or "").strip() or "未命名",
            "content": (data.get("content") or "").strip(),
            "category": data.get("category") or "instruction",
            "tags": [t.strip() for t in (data.get("tags") or []) if t and t.strip()],
            "note": (data.get("note") or "").strip(),
            "created_at": now,
            "updated_at": now
        }
        prompts.append(new_p)
        self._write(prompts)
        return new_p

    def delete_prompt(self, pid: str) -> bool:
        prompts = self._read()
        new_list = [p for p in prompts if p.get("id") != pid]
        if len(new_list) == len(prompts):
            return False
        self._write(new_list)
        return True

    # ========== 预置默认 prompt（首次使用自动注入）==========
    def ensure_defaults(self):
        if self._read():
            return
        defaults = [
            {
                "title": "🎨 润色流畅（通用）",
                "content": "润色本段，使语言更流畅自然，避免AI腔和生硬表达，保持原文叙事节奏和视角。",
                "category": "instruction",
                "tags": ["润色", "通用", "流畅"]
            },
            {
                "title": "🌱 扩写细节 +30%",
                "content": "扩写这段，增加必要的场景描写、动作细节和心理活动，字数增加约30%，保持剧情不拖沓。",
                "category": "instruction",
                "tags": ["扩写", "细节"]
            },
            {
                "title": "✂️ 精简浓缩",
                "content": "精简本段，删除水字数的冗余修饰和重复词，保留核心剧情和关键信息。",
                "category": "instruction",
                "tags": ["精简", "浓缩"]
            },
            {
                "title": "💬 对话优化",
                "content": "优化这段对话，让对话更符合角色性格和身份，并增加潜台词和动作描写，避免一问一答式。",
                "category": "instruction",
                "tags": ["对话", "角色"]
            },
            {
                "title": "🧹 AI腔清理",
                "content": "检查本段的AI腔问题，改得更像真人写作，去除生硬的过渡词（如：然而、于是、接着）和过度形容词堆砌。",
                "category": "instruction",
                "tags": ["AI腔", "清理"]
            },
            {
                "title": "古风文风设定",
                "content": "整体文风偏古风雅致，避免现代词、网络用语和白话翻译腔；用词含蓄有韵味，对话半文半白符合古代语境。",
                "category": "global",
                "tags": ["古风", "文风"]
            },
            {
                "title": "权谋文专属 Writer",
                "content": "你是一位擅长写权谋斗争的资深网文编辑。笔下人物智商在线，对话暗含机锋，布局有伏笔有回收。修改时优先考虑权力逻辑、人物立场和因果链条，避免降智和巧合推动剧情。",
                "category": "agent",
                "tags": ["权谋", "Agent设定"]
            },
            {
                "title": "甜宠文专属 Writer",
                "content": "你是一位擅长写甜宠言情的资深编辑。笔触细腻柔软，互动有糖分但不油腻，主角情感推进自然有层次感，避免工业糖精和生硬撒糖。",
                "category": "agent",
                "tags": ["甜宠", "Agent设定"]
            }
        ]
        prompts = []
        now = int(time.time())
        for i, d in enumerate(defaults):
            d2 = dict(d)
            d2["id"] = uuid.uuid4().hex[:10]
            d2["created_at"] = now - i
            d2["updated_at"] = now - i
            d2["note"] = "系统默认模板，可编辑可删除"
            prompts.append(d2)
        self._write(prompts)
