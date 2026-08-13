from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from config.settings import NOVELS_DIR
from core.chapter_parser import parse_chapters, chapter_to_paragraphs
from core.chroma_memory import ChromaMemory

META_FILE = "novel_meta.json"


class NovelService:
    def __init__(self):
        self.base_dir = NOVELS_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def list_novels(self) -> List[Dict]:
        novels = []
        for d in self.base_dir.iterdir():
            if not d.is_dir():
                continue
            meta_path = d / META_FILE
            if meta_path.exists():
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    novels.append(meta)
                except Exception:
                    novels.append({"id": d.name, "title": d.name, "chapters": 0})
            else:
                novels.append({"id": d.name, "title": d.name, "chapters": 0})
        novels.sort(key=lambda x: x.get("imported_at", 0), reverse=True)
        return novels

    def import_txt(self, file_path: str, title: Optional[str] = None, auto_index: bool = True) -> Dict:
        src = Path(file_path)
        if not src.exists():
            return {"error": "文件不存在"}
        try:
            raw = src.read_text(encoding=self._detect_encoding(src))
        except Exception as e:
            return {"error": f"文件读取失败：{str(e)}"}
        if not raw.strip():
            return {"error": "文件内容为空"}
        novel_title = title or src.stem
        novel_id = hashlib.md5(novel_title.encode("utf-8")).hexdigest()[:12]
        novel_dir = self.base_dir / novel_id
        if novel_dir.exists():
            shutil.rmtree(novel_dir, ignore_errors=True)
        try:
            novel_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, novel_dir / src.name)
        except Exception as e:
            return {"error": f"准备数据目录失败：{str(e)}"}

        try:
            chapters = parse_chapters(raw)
        except Exception as e:
            return {"error": f"章节解析失败：{str(e)}"}
        if not chapters:
            return {"error": "未能识别到任何章节，请检查TXT是否有标准的章节标题（如：第1章 楔子）"}
        for ch in chapters:
            ch["paragraphs"] = chapter_to_paragraphs(ch["content"])
            ch["paragraph_count"] = len(ch["paragraphs"])

        self._save_novel_data(novel_dir, chapters)

        meta = {
            "id": novel_id,
            "title": novel_title,
            "source_file": src.name,
            "chapters": len(chapters),
            "total_paragraphs": sum(c["paragraph_count"] for c in chapters),
            "imported_at": int(__import__("time").time()),
            "indexed": False
        }
        with open(novel_dir / META_FILE, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        index_warning = None
        if auto_index:
            index_warning = self._build_index(novel_id, chapters, meta)
            if index_warning is None:
                meta["indexed"] = True
                with open(novel_dir / META_FILE, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)

        result = {"id": novel_id, "title": novel_title, "chapters": len(chapters), "indexed": meta["indexed"]}
        if index_warning:
            result["index_warning"] = index_warning
        return result

    def _save_novel_data(self, novel_dir: Path, chapters: List[Dict]):
        data_dir = novel_dir / "chapters"
        data_dir.mkdir(exist_ok=True)
        for ch in chapters:
            ch_data = {
                "index": ch["index"],
                "title": ch["title"],
                "content": ch["content"],
                "paragraphs": ch.get("paragraphs", []),
                "modified_content": ch["content"],
                "modified_paragraphs": ch.get("paragraphs", [])
            }
            with open(data_dir / f"chapter_{ch['index']:05d}.json", "w", encoding="utf-8") as f:
                json.dump(ch_data, f, ensure_ascii=False, separators=(",", ":"))
        toc = [{"index": c["index"], "title": c["title"], "paragraph_count": c.get("paragraph_count", 0)} for c in chapters]
        with open(novel_dir / "toc.json", "w", encoding="utf-8") as f:
            json.dump(toc, f, ensure_ascii=False, indent=2)

    def _build_index(self, novel_id: str, chapters: List[Dict], meta: Dict):
        """尝试构建向量索引。失败时返回警告字符串，不阻断主流程；成功返回 None"""
        try:
            mem = ChromaMemory(novel_id)
            mem.reset()
            total = len(chapters)
            for ch in chapters:
                mem.index_chapter(ch["index"], ch["title"], ch["content"], total_chapters=total)
            return None
        except Exception as e:
            # 索引失败不影响章节导入，只记录警告
            msg = str(e)
            if "chromadb" in msg.lower() or "module not found" in msg.lower():
                msg = "向量索引未构建（ChromaDB/SentenceTransformer 未安装）。章节已正常导入，可使用基础编辑功能；AI 智能检索、伏笔联想等功能需安装依赖：双击 安装依赖.bat"
            else:
                msg = f"向量索引构建失败：{msg}。章节已正常导入，可稍后通过「重索引」按钮重试。"
            return msg

    def get_novel_meta(self, novel_id: str) -> Optional[Dict]:
        meta_path = self.base_dir / novel_id / META_FILE
        if not meta_path.exists():
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def get_toc(self, novel_id: str) -> List[Dict]:
        toc_path = self.base_dir / novel_id / "toc.json"
        if not toc_path.exists():
            return []
        try:
            with open(toc_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def get_chapter(self, novel_id: str, chapter_idx: int) -> Optional[Dict]:
        pattern = f"chapter_{chapter_idx:05d}.json"
        files = list((self.base_dir / novel_id / "chapters").glob(pattern))
        if not files:
            files = sorted((self.base_dir / novel_id / "chapters").glob("chapter_*.json"))
            target = None
            for f in files:
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        d = json.load(fh)
                        if d.get("index") == chapter_idx:
                            target = f
                            break
                except Exception:
                    continue
            if not target:
                return None
            files = [target]
        try:
            with open(files[0], "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("modified_content", data.get("content", ""))
            data.setdefault("modified_paragraphs", data.get("paragraphs", []))
            return data
        except Exception:
            return None

    def search_text(self, novel_id: str, keyword: str, start: Optional[int] = None,
                    end: Optional[int] = None, case_sensitive: bool = False) -> List[Dict]:
        """纯文本检索：在所有章节中查找关键词，返回包含关键词的章节及上下文片段。"""
        toc = self.get_toc(novel_id)
        if not toc or not keyword:
            return []
        kw = keyword if case_sensitive else keyword.lower()
        results = []
        for item in toc:
            idx = item["index"]
            if start is not None and idx < start:
                continue
            if end is not None and idx > end:
                continue
            ch = self.get_chapter(novel_id, idx)
            if not ch:
                continue
            content = ch.get("modified_content") or ch.get("content") or ""
            if not content and not item.get("title"):
                continue
            hay = content if case_sensitive else content.lower()
            matches = []
            title_hit = False
            title = item.get("title", "")
            # 标题命中（优先展示，标记 in_title）
            if kw and kw in (title if case_sensitive else title.lower()):
                title_hit = True
                matches.append({"context": "【标题】" + title, "pos": -1, "in_title": True})
            pos = 0
            while True:
                p = hay.find(kw, pos)
                if p == -1:
                    break
                cs = max(0, p - 50)
                ce = min(len(content), p + len(keyword) + 50)
                matches.append({"context": content[cs:ce], "pos": p})
                pos = p + len(kw)
                if len(matches) >= 30:
                    break
            if matches:
                results.append({
                    "index": idx,
                    "title": title,
                    "paragraph_count": item.get("paragraph_count", 0),
                    "match_count": len(matches),
                    "title_hit": title_hit,
                    "matches": matches
                })
        return results

    def save_chapter(self, novel_id: str, chapter_idx: int, data: Dict,
                     auto_mark_title: bool = True) -> bool:
        ch = self.get_chapter(novel_id, chapter_idx)
        if ch is None:
            return False
        old_title = ch.get("title", "")
        content_changed = False
        if "modified_content" in data:
            new_mod = data.get("modified_content", ch.get("modified_content", ""))
            old_content = ch.get("content", "")
            ch["modified_content"] = new_mod
            ch["modified_paragraphs"] = chapter_to_paragraphs(new_mod)
            content_changed = new_mod != old_content
            # 保存即定稿：原文同步为修改版，保证保存后原文部分也显示最新内容
            ch["content"] = ch["modified_content"]
            ch["paragraphs"] = ch["modified_paragraphs"]
        new_title = data.get("title")
        if new_title and new_title.strip() and new_title.strip() != old_title:
            ch["title"] = new_title.strip()
        # 内容被修改且未显式改名时，自动给标题加【已修改】后缀（去重）
        # auto_mark_title=False 用于词汇替换等场景（不自动加标记）
        if content_changed and auto_mark_title and not (new_title and new_title.strip()):
            title = ch.get("title", "")
            if title and "【已修改】" not in title:
                ch["title"] = title + "【已修改】"
        # 标题有变化则同步目录
        if ch.get("title", "") != old_title:
            self._update_toc_title(novel_id, chapter_idx, ch["title"])
        pattern = f"chapter_{chapter_idx:05d}.json"
        files = list((self.base_dir / novel_id / "chapters").glob(pattern))
        if not files:
            return False
        try:
            with open(files[0], "w", encoding="utf-8") as f:
                # 紧凑序列化：章节文件含大段文本与上千段落数组，indent=2 会导致保存/读取显著变慢
                json.dump(ch, f, ensure_ascii=False, separators=(",", ":"))
            return True
        except Exception:
            return False

    def _update_toc_title(self, novel_id: str, chapter_idx: int, title: str):
        """更新章节目录中对应章节的标题。"""
        toc = self.get_toc(novel_id)
        for item in toc:
            if item["index"] == chapter_idx:
                item["title"] = title
                break
        toc_path = self.base_dir / novel_id / "toc.json"
        try:
            toc_path.write_text(json.dumps(toc, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass

    def reindex_chapter(self, novel_id: str, chapter_idx: int) -> Dict:
        ch = self.get_chapter(novel_id, chapter_idx)
        if not ch:
            return {"ok": False, "error": "章节不存在"}
        try:
            mem = ChromaMemory(novel_id)
            cnt = mem.index_chapter(chapter_idx, ch.get("title", ""), ch.get("modified_content", ch.get("content", "")))
            return {"ok": True, "chunks": cnt}
        except Exception as e:
            msg = str(e)
            if "chromadb" in msg.lower() or "module not found" in msg.lower():
                msg = "ChromaDB/SentenceTransformer 未安装。请先执行 安装依赖.bat 后再使用向量记忆功能。"
            return {"ok": False, "error": msg}

    def export_novel(self, novel_id: str, use_modified: bool = True) -> str:
        toc = self.get_toc(novel_id)
        meta = self.get_novel_meta(novel_id) or {}
        parts = [f"{meta.get('title', '')}\n\n"]
        for item in toc:
            ch = self.get_chapter(novel_id, item["index"])
            if not ch:
                continue
            title = ch.get("title", f"第{item['index']}章")
            body = ch.get("modified_content") if use_modified else ch.get("content", "")
            parts.append(f"第{ch.get('index', item['index'])}章 {title}\n\n{body}\n\n")
        content = "\n".join(parts)
        out = self.base_dir / novel_id / f"{meta.get('title', novel_id)}_修改版.txt"
        out.write_text(content, encoding="utf-8")
        return str(out)

    def delete_novel(self, novel_id: str) -> bool:
        d = self.base_dir / novel_id
        if not d.exists():
            return False
        shutil.rmtree(d, ignore_errors=True)
        try:
            mem_path = NOVELS_DIR.parent / "chroma_db" / novel_id
            if mem_path.exists():
                shutil.rmtree(mem_path, ignore_errors=True)
        except Exception:
            pass
        return True

    @staticmethod
    def _detect_encoding(path: Path) -> str:
        for enc in ["utf-8", "utf-8-sig", "gbk", "gb18030", "big5"]:
            try:
                path.read_text(encoding=enc)
                return enc
            except UnicodeDecodeError:
                continue
        return "utf-8"
