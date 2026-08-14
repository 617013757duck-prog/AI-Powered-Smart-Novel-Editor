from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import List, Dict, Optional

# 阻止 chromadb 的 ONNX Runtime 尝试访问 GPU/NVIDIA 驱动
# 避免因驱动文件访问受限导致 C++ 访问冲突 (0xC0000005) 崩溃整个进程
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("ORT_DISABLE_CUDA", "1")
os.environ.setdefault("ONNX_MODE", "CPU")

from config.settings import CHROMA_DIR, load_config


class ChromaMemory:
    _instances: Dict[str, "ChromaMemory"] = {}

    def __new__(cls, novel_id: str = "global"):
        if novel_id not in cls._instances:
            inst = super().__new__(cls)
            inst._initialized = False
            cls._instances[novel_id] = inst
        return cls._instances[novel_id]

    def __init__(self, novel_id: str = "global"):
        if getattr(self, "_initialized", False):
            return
        self.novel_id = novel_id
        cfg = load_config()
        self.chunk_size = cfg["embedding"]["chunk_size"]
        self.chunk_overlap = cfg["embedding"]["chunk_overlap"]
        self.top_k = cfg["tri_ai"]["retrieve_top_k"]
        self.client = None
        self.collection = None
        self.embedding_fn = None
        self._init_failed = False  # 延迟初始化标志：不在此处调用 _setup_client()
        self._initialized = True

    def _ensure_client(self):
        """延迟初始化 ChromaDB：只有真正需要向量操作时才创建连接。
        注意：此方法仅在 index_chapter / retrieve / update_chunk 等需要
        真实向量操作的方法中调用。stats() 等只需返回计数的端点不触发初始化。"""
        if self.client is not None or self._init_failed:
            return self.client is not None
        try:
            self._setup_client()
            return True
        except Exception:
            self._init_failed = True
            return False

    def _setup_client(self):
        try:
            import chromadb
        except ImportError as e:
            raise RuntimeError(
                "ChromaDB 未安装。请先执行 安装依赖.bat 安装 chromadb 等依赖包。"
                f" 原错误: {e}"
            )
        persist_dir = str(CHROMA_DIR / self.novel_id)
        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=persist_dir)

        # 使用内置 DefaultEmbeddingFunction（chromadb 自带 ONNX 模型，无需下载）
        from chromadb.utils import embedding_functions
        ef = embedding_functions.DefaultEmbeddingFunction()

        cfg_model = load_config()["embedding"]["model_name"]
        if cfg_model and cfg_model not in ("default", "all-MiniLM-L6-v2", ""):
            # 只有用户明确设置了自定义模型名时才尝试下载
            try:
                ef = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name=cfg_model
                )
            except Exception:
                pass  # 下载失败则保持 DefaultEmbeddingFunction
        self.embedding_fn = ef
        kwargs = {"name": f"novel_{self.novel_id}", "metadata": {"hnsw:space": "cosine"}}
        if self.embedding_fn is not None:
            kwargs["embedding_function"] = self.embedding_fn
        self.collection = self.client.get_or_create_collection(**kwargs)

    def reset(self):
        if not self._ensure_client():
            self._initialized = False
            self.__init__(self.novel_id)
            return
        try:
            self.client.delete_collection(name=f"novel_{self.novel_id}")
        except Exception:
            pass
        self._initialized = False
        self.__init__(self.novel_id)

    @staticmethod
    def chunk_text(text: str, chunk_size: int = 500, chunk_overlap: int = 50) -> List[str]:
        if not text:
            return []
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        chunks = []
        current = ""
        for para in paragraphs:
            if len(current) + len(para) + 2 <= chunk_size:
                current = (current + "\n\n" + para).strip() if current else para
            else:
                if current:
                    chunks.append(current)
                if len(para) > chunk_size:
                    start = 0
                    while start < len(para):
                        end = start + chunk_size
                        piece = para[start:end]
                        chunks.append(piece)
                        start = end - chunk_overlap if end < len(para) else end
                else:
                    current = para
        if current:
            chunks.append(current)
        return chunks if chunks else [text]

    def index_chapter(self, chapter_idx: int, chapter_title: str, chapter_content: str,
                      total_chapters: int = 0) -> int:
        if not self._ensure_client():
            return 0
        chunks = self.chunk_text(chapter_content, self.chunk_size, self.chunk_overlap)
        ids, docs, metas = [], [], []
        for i, chunk in enumerate(chunks):
            cid = hashlib.md5(f"{self.novel_id}|{chapter_idx}|{i}|{chunk[:80]}".encode("utf-8")).hexdigest()
            ids.append(cid)
            docs.append(chunk)
            metas.append({
                "novel_id": self.novel_id,
                "chapter_idx": chapter_idx,
                "chapter_title": chapter_title,
                "chunk_idx": i,
                "total_chunks": len(chunks),
                "total_chapters": total_chapters,
                "chunk_preview": chunk[:60]
            })
        if ids:
            self.collection.upsert(ids=ids, documents=docs, metadatas=metas)
        return len(chunks)

    def retrieve(self, query: str, top_k: Optional[int] = None,
                 chapter_idx: Optional[int] = None,
                 exclude_chapter: bool = False) -> List[Dict]:
        if not self._ensure_client():
            return []
        k = top_k or self.top_k
        where = None
        if chapter_idx is not None and exclude_chapter:
            where = {"chapter_idx": {"$ne": int(chapter_idx)}}
        # 结果合并：关键词精确命中优先，向量语义结果补充。
        # 说明：默认内置 embedding（all-MiniLM-L6-v2）对中文几乎无区分度，
        # 仅靠向量检索会"检索不到"，因此叠加关键词匹配保证中文可检索。
        merged: Dict[str, dict] = {}
        try:
            kw_items = self._keyword_retrieve(query, k, chapter_idx, exclude_chapter)
            for it in kw_items:
                merged[it["id"]] = it
        except Exception:
            pass
        try:
            result = self.collection.query(
                query_texts=[query],
                n_results=max(1, min(k, max(1, self.collection.count()))),
                where=where
            )
            ids = result.get("ids", [[]])[0]
            docs = result.get("documents", [[]])[0]
            metas = result.get("metadatas", [[]])[0]
            dists = result.get("distances", [[]])[0]
            for i in range(len(ids)):
                score = 1.0 - (dists[i] if dists[i] is not None else 1.0)
                merged.setdefault(ids[i], {
                    "id": ids[i],
                    "content": docs[i],
                    "meta": metas[i] or {},
                    "score": round(float(score), 4)
                })
        except Exception:
            pass
        items = list(merged.values())
        # 关键词命中( kw_hits )优先展示，其次按向量相关度
        items.sort(key=lambda x: (x.get("kw_hits", 0), x.get("score", 0)), reverse=True)
        return items[:k]

    def _extract_keywords(self, query: str, max_words: int = 8) -> List[str]:
        """从检索词中提取有区分度的关键词（长度≥2，跳过空白与常见虚词）。"""
        q = (query or "").strip()
        if not q:
            return []
        parts = re.split(r"[\s,，。、；;:：!！?？\"'“”‘’()（）《》<>·\-—\n]+", q)
        kws = [p for p in parts if len(p) >= 2]
        # 常见虚词/停用词，过滤掉以减少噪音
        stop = {"什么", "怎么", "一个", "这个", "那个", "可以", "还有", "没有", "就是",
                "我们", "你们", "他们", "已经", "时候", "因为", "所以", "但是", "不是",
                "现在", "这样", "那样", "如果", "然后", "章节", "内容"}
        kws = [w for w in kws if w not in stop]
        if not kws:
            kws = [q] if len(q) >= 2 else []
        return kws[:max_words]

    def _keyword_retrieve(self, query: str, k: int,
                          chapter_idx: Optional[int] = None,
                          exclude_chapter: bool = False) -> List[Dict]:
        """基于关键词精确匹配的检索：遍历向量库全部块，统计命中词次数。"""
        kws = self._extract_keywords(query)
        if not kws:
            return []
        try:
            all_data = self.collection.get(include=["documents", "metadatas"])
        except Exception:
            return []
        ids = all_data.get("ids", [])
        docs = all_data.get("documents", [])
        metas = all_data.get("metadatas", [])
        hits = []
        for i, doc in enumerate(docs):
            meta = metas[i] or {}
            if chapter_idx is not None and exclude_chapter and meta.get("chapter_idx") == int(chapter_idx):
                continue
            text = doc or ""
            cnt = 0
            for kw in kws:
                cnt += text.count(kw)
            if cnt:
                hits.append({
                    "id": ids[i],
                    "content": text,
                    "meta": meta,
                    "score": 1.0,
                    "kw_hits": cnt
                })
        hits.sort(key=lambda x: x["kw_hits"], reverse=True)
        return hits[:k]

    def retrieve_by_chapter(self, chapter_idx: int, limit: int = 50) -> List[Dict]:
        if not self._ensure_client():
            return []
        try:
            result = self.collection.get(
                where={"chapter_idx": int(chapter_idx)},
                include=["documents", "metadatas"]
            )
        except Exception:
            return []
        items = []
        ids = result.get("ids", [])
        docs = result.get("documents", [])
        metas = result.get("metadatas", [])
        for i in range(len(ids)):
            meta = metas[i] or {}
            items.append({
                "id": ids[i],
                "content": docs[i],
                "meta": meta,
                "chunk_idx": meta.get("chunk_idx", 0)
            })
        items.sort(key=lambda x: x["chunk_idx"])
        return items[:limit]

    def update_chunk(self, chunk_id: str, new_content: str, meta_extra: Optional[Dict] = None):
        if not self._ensure_client():
            return False
        try:
            old = self.collection.get(ids=[chunk_id], include=["metadatas"])
            if not old["ids"]:
                return False
            meta = old["metadatas"][0] or {}
            if meta_extra:
                meta.update(meta_extra)
            meta["chunk_preview"] = new_content[:60]
            meta["modified"] = 1
            self.collection.upsert(ids=[chunk_id], documents=[new_content], metadatas=[meta])
            return True
        except Exception:
            return False

    def stats(self) -> Dict:
        # 主动尝试初始化并返回真实块数；chromadb 不可用时降级返回 0
        if self._ensure_client():
            try:
                return {
                    "total_chunks": self.collection.count(),
                    "novel_id": self.novel_id
                }
            except Exception:
                pass
        return {"total_chunks": 0, "novel_id": self.novel_id}
