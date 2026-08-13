from __future__ import annotations

import json
import os
import sys
import subprocess
import platform
import threading
import time
from pathlib import Path

# === 必须在所有其他 import 之前设置（包括 chromadb 的间接导入） ===
# 阻止 ONNX Runtime 尝试访问 GPU/NVIDIA 驱动，避免 C++ 访问冲突崩溃
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
os.environ["NVIDIA_VISIBLE_DEVICES"] = ""
os.environ["ORT_DISABLE_CUDA"] = "1"
os.environ["ORT_DISABLE_TENSORRT"] = "1"
os.environ["ORT_DISABLE_CANN"] = "1"
os.environ["ONNX_MODE"] = "CPU"
os.environ["OMP_NUM_THREADS"] = "1"
# 禁用 chromadb/onnxruntime 遥测
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["CHROMA_TELEMETRY_IMPL"] = "none"
os.environ["CHROMA_TELEMETRY"] = "OFF"
# 禁止 sentence-transformers 下载模型时访问 HuggingFace（绕过网络限制）
os.environ["SENTENCE_TRANSFORMERS_HOME"] = str(Path(__file__).resolve().parent / "data" / "st_models")
# 国内 HuggingFace 镜像加速（避免模型下载阻塞导致"网络错误"）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config.settings import load_config, save_config, DATA_DIR, NOVELS_DIR, DEFAULT_CONFIG
from core.ollama_client import OllamaClient
from core.chroma_memory import ChromaMemory
from core.tri_ai import TriModelAI
from core.chapter_parser import chapter_to_paragraphs
from services.novel_service import NovelService
from services.prompt_service import PromptService

STATIC_DIR = ROOT / "static"
app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
CORS(app)

novel_svc = NovelService()
_global_ollama = OllamaClient()
_prompt_svc = PromptService()
_prompt_svc.ensure_defaults()


def _json_error(message: str, status: int = 400, **extra):
    payload = {"error": message}
    payload.update(extra)
    return jsonify(payload), status


@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


# 显式静态资源路由（确保 CSS/JS 可访问，避免 static_url_path 配置问题）
@app.route("/css/<path:filename>")
def serve_css(filename):
    return send_from_directory(str(STATIC_DIR / "css"), filename)


@app.route("/js/<path:filename>")
def serve_js(filename):
    return send_from_directory(str(STATIC_DIR / "js"), filename)


@app.errorhandler(404)
def _spa_404(e):
    # API 请求返回 JSON 404；前端页面请求返回 SPA 首页
    if request.path.startswith("/api/"):
        return _json_error(f"接口不存在：{request.path}", 404)
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.errorhandler(500)
def _handle_500(e):
    # 所有 500 错误返回 JSON（而不是默认 HTML 错误页）
    msg = str(getattr(e, "original_exception", e) or e)
    if not msg or msg.startswith("<"):
        msg = "服务器内部错误"
    return _json_error(msg, 500, exception_type=type(e).__name__)


@app.errorhandler(Exception)
def _handle_exception(e):
    # 兜底：任何未捕获的异常都返回 JSON
    msg = str(e)
    if isinstance(e, RuntimeError) and ("chromadb" in msg.lower() or "未安装" in msg):
        return _json_error(msg, 503, exception_type="DependencyMissing")
    return _json_error(msg, 500, exception_type=type(e).__name__)


# ========== 配置与连接检查 ==========
@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_config())


def _merge_preserve(base: dict, override: dict) -> dict:
    """深度合并：override 缺失的键保留 base 的值，避免丢失多槽位等结构。"""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_preserve(out[k], v)
        else:
            out[k] = v
    return out


@app.route("/api/config", methods=["POST"])
def api_save_config():
    incoming = request.json or {}
    cfg = load_config()
    merged = _merge_preserve(cfg, incoming)
    # 外部 API 槽位一致性：表单字段同步写入当前槽位，避免顶层与槽位漂移
    ext_in = incoming.get("external_api")
    if isinstance(ext_in, dict):
        ext = merged.setdefault("external_api", {})
        if not isinstance(ext.get("slots"), dict) or not ext["slots"]:
            ext["slots"] = {"default": {}}
            ext["active_slot"] = ext.get("active_slot") or "default"
        active = ext.get("active_slot") or "default"
        slot = ext["slots"].setdefault(active, {})
        for k in ("base_url", "api_key", "model", "timeout", "temperature"):
            if k in ext_in:
                slot[k] = ext_in[k]
    save_config(merged)
    return jsonify({"ok": True})


@app.route("/api/config/_reset", methods=["POST"])
def api_reset_config():
    save_config(json.loads(json.dumps(DEFAULT_CONFIG)))
    return jsonify({"ok": True})


@app.route("/api/novels/_open_dir", methods=["POST"])
def api_open_dir():
    data = request.json or {}
    path = data.get("path")
    if not path:
        return jsonify({"ok": False})
    try:
        p = Path(path)
        target = str(p.parent if p.is_file() else p)
        if platform.system() == "Windows":
            os.startfile(target)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/ollama/check", methods=["GET"])
def api_check_ollama():
    return jsonify(_global_ollama.check_connection())


@app.route("/api/models/available", methods=["GET"])
def api_list_models():
    """列出 Ollama 本地所有已下载模型（用于设置里一键切换下拉）"""
    return jsonify(_global_ollama.list_models())


@app.route("/api/ai/healthz", methods=["GET"])
def api_ai_healthz():
    """GPU/AI 真实运行心跳：真实调用一次 generate 验证 AI 在跑"""
    return jsonify(_global_ollama.healthz_gpu())


# ========== Prompt 仓库 ==========
@app.route("/api/prompts", methods=["GET"])
def api_list_prompts():
    category = request.args.get("category")
    keyword = request.args.get("keyword")
    tag = request.args.get("tag")
    return jsonify(_prompt_svc.list_prompts(category=category, keyword=keyword, tag=tag))


@app.route("/api/prompts/tags", methods=["GET"])
def api_prompt_tags():
    return jsonify(_prompt_svc.all_tags())


@app.route("/api/prompts/<pid>", methods=["GET"])
def api_get_prompt(pid):
    p = _prompt_svc.get_prompt(pid)
    if not p:
        return _json_error("未找到该 prompt", 404)
    return jsonify(p)


@app.route("/api/prompts", methods=["POST"])
def api_save_prompt():
    data = request.json or {}
    saved = _prompt_svc.save_prompt(data)
    return jsonify({"ok": True, "prompt": saved})


@app.route("/api/prompts/<pid>", methods=["DELETE"])
def api_delete_prompt(pid):
    ok = _prompt_svc.delete_prompt(pid)
    return jsonify({"ok": ok})


# ========== 小说管理 ==========
@app.route("/api/novels", methods=["GET"])
def api_list_novels():
    return jsonify(novel_svc.list_novels())


@app.route("/api/novels/import", methods=["POST"])
def api_import_novel():
    data = request.json or {}
    file_path = data.get("file_path")
    title = data.get("title")
    auto_index = data.get("auto_index", True)
    if not file_path:
        return jsonify({"error": "缺少file_path"}), 400
    res = novel_svc.import_txt(file_path, title=title, auto_index=auto_index)
    return jsonify(res)


@app.route("/api/novels/upload", methods=["POST"])
def api_upload_novel():
    f = request.files.get("file")
    if not f:
        return _json_error("未找到文件", 400)
    tmp = DATA_DIR / "_uploads"
    dest = None
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        dest = tmp / (Path(f.filename).name if f.filename else f"upload_{int(__import__('time').time())}.txt")
        f.save(str(dest))
        title = request.form.get("title") or Path(str(f.filename)).stem
        res = novel_svc.import_txt(str(dest), title=title, auto_index=True)
        if isinstance(res, dict) and res.get("error"):
            return _json_error(res["error"], 400)
        return jsonify(res)
    except PermissionError as pe:
        return _json_error(f"文件被占用，请关闭小说文件后重试：{str(pe)}", 400)
    except Exception as e:
        return _json_error(f"导入异常：{str(e)}", 500)
    finally:
        if dest is not None:
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass


@app.route("/api/novels/<novel_id>", methods=["GET"])
def api_get_novel_meta(novel_id):
    meta = novel_svc.get_novel_meta(novel_id)
    if not meta:
        return jsonify({"error": "未找到"}), 404
    toc = novel_svc.get_toc(novel_id)
    return jsonify({"meta": meta, "toc": toc})


@app.route("/api/novels/<novel_id>", methods=["DELETE"])
def api_delete_novel(novel_id):
    ok = novel_svc.delete_novel(novel_id)
    return jsonify({"ok": ok})


# ========== 按小说区分的 Agent 设置（自动保存/读取） ==========
@app.route("/api/novels/<novel_id>/agent_settings", methods=["GET"])
def api_get_agent_settings(novel_id):
    """读取某本小说的 Agent 设置（全局指令、自定义Agent、最近修改要求）"""
    config_path = NOVELS_DIR / novel_id / "agent_settings.json"
    if not config_path.exists():
        return jsonify({
            "novel_id": novel_id,
            "global_prompt": "",
            "custom_agent": "",
            "last_instruction": "",
            "saved_slots": []  # 用户手动保存的 3 个槽位
        })
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("saved_slots", [])
        return jsonify(data)
    except Exception:
        return jsonify({
            "novel_id": novel_id,
            "global_prompt": "",
            "custom_agent": "",
            "last_instruction": "",
            "saved_slots": []
        })


@app.route("/api/novels/<novel_id>/agent_settings", methods=["POST"])
def api_save_agent_settings(novel_id):
    """保存某本小说的 Agent 设置"""
    data = request.json or {}
    config_path = NOVELS_DIR / novel_id / "agent_settings.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # 合并：不覆盖已有字段
    existing = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass
    merged = {
        "novel_id": novel_id,
        "global_prompt": data.get("global_prompt", existing.get("global_prompt", "")),
        "custom_agent": data.get("custom_agent", existing.get("custom_agent", "")),
        "last_instruction": data.get("last_instruction", existing.get("last_instruction", "")),
        "saved_slots": data.get("saved_slots", existing.get("saved_slots", []))
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True, "settings": merged})


@app.route("/api/novels/<novel_id>/toc", methods=["GET"])
def api_get_toc(novel_id):
    return jsonify(novel_svc.get_toc(novel_id))


@app.route("/api/novels/<novel_id>/chapters/<int:idx>", methods=["GET"])
def api_get_chapter(novel_id, idx):
    ch = novel_svc.get_chapter(novel_id, idx)
    if not ch:
        return jsonify({"error": "未找到章节"}), 404
    # 合并「总体设定修改」的待确认内容（用户阅览时不丢失已修改内容）
    pending = _bulk_pending.get(novel_id, {})
    if idx in pending:
        p = pending[idx]
        ch["pending_mod"] = True
        ch["modified_content"] = p.get("modified", ch.get("modified_content", ""))
        ch["modified_paragraphs"] = p.get("modified_paragraphs", ch.get("modified_paragraphs", []))
    return jsonify(ch)


@app.route("/api/novels/<novel_id>/chapters/<int:idx>", methods=["PUT"])
def api_save_chapter(novel_id, idx):
    data = request.json or {}
    ok = novel_svc.save_chapter(novel_id, idx, data)
    return jsonify({"ok": ok})


@app.route("/api/novels/<novel_id>/chapters/<int:idx>/reindex", methods=["POST"])
def api_reindex_chapter(novel_id, idx):
    return jsonify(novel_svc.reindex_chapter(novel_id, idx))


@app.route("/api/novels/<novel_id>/export", methods=["GET"])
def api_export_novel(novel_id):
    use_mod = request.args.get("modified", "1") == "1"
    path = novel_svc.export_novel(novel_id, use_modified=use_mod)
    fname = Path(path).name
    return jsonify({"ok": True, "file": path, "filename": fname})


@app.route("/api/novels/<novel_id>/memory/stats", methods=["GET"])
def api_memory_stats(novel_id):
    mem = ChromaMemory(novel_id)
    return jsonify(mem.stats())


@app.route("/api/novels/<novel_id>/memory/retrieve", methods=["POST"])
def api_memory_retrieve(novel_id):
    data = request.json or {}
    query = data.get("query", "")
    top_k = int(data.get("top_k", 5))
    ch_idx = data.get("chapter_idx")
    exc = data.get("exclude_chapter", True)
    mem = ChromaMemory(novel_id)
    return jsonify(mem.retrieve(query=query, top_k=top_k, chapter_idx=ch_idx, exclude_chapter=exc))


# ========== 三模态AI ==========
@app.route("/api/ai/writer/rewrite", methods=["POST"])
def ai_writer_rewrite():
    data = request.json or {}
    novel_id = data.get("novel_id")
    if not novel_id:
        return jsonify({"error": "缺少novel_id"}), 400
    stream = data.get("stream", False)
    if stream:
        def gen():
            try:
                ai = TriModelAI(novel_id)  # 移入 generator 内，避免初始化异常返回 500
            except Exception as e:
                yield "data: " + json.dumps({"error": f"AI 初始化失败: {str(e)}", "done": True}, ensure_ascii=False) + "\n\n"
                return
            g = ai.writer_rewrite(
                original_text=data.get("original_text", ""),
                instruction=data.get("instruction", ""),
                chapter_idx=data.get("chapter_idx"),
                chapter_title=data.get("chapter_title", ""),
                global_prompt=data.get("global_prompt", ""),
                custom_instruction=data.get("custom_instruction", ""),
                stream=True
            )
            try:
                for chunk in g:
                    yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
            except Exception as e:
                yield "data: " + json.dumps({"error": str(e), "done": True}, ensure_ascii=False) + "\n\n"
        return Response(stream_with_context(gen()), mimetype="text/event-stream")
    try:
        ai = TriModelAI(novel_id)
    except Exception as e:
        return _json_error(f"AI 初始化失败: {str(e)}", 500)
    res = ai.writer_rewrite(
        original_text=data.get("original_text", ""),
        instruction=data.get("instruction", ""),
        chapter_idx=data.get("chapter_idx"),
        chapter_title=data.get("chapter_title", ""),
        global_prompt=data.get("global_prompt", ""),
        custom_instruction=data.get("custom_instruction", "")
    )
    return jsonify(res)


@app.route("/api/ai/writer/rewrite_batch", methods=["POST"])
def ai_writer_rewrite_batch():
    """
    统一批量改写：将所有段落合并为一次 AI 调用，通读全文后再改写。
    SSE：progress/chunk/result/done（多段语义，单段流式，多段统一）
    """
    data = request.json or {}
    novel_id = data.get("novel_id")
    paragraphs = data.get("paragraphs") or []
    instruction = data.get("instruction", "")
    global_prompt = data.get("global_prompt", "")
    custom_instruction = data.get("custom_instruction", "")
    chapter_idx = data.get("chapter_idx")
    chapter_title = data.get("chapter_title", "")

    if not novel_id:
        return _json_error("缺少novel_id")
    if not paragraphs:
        return _json_error("paragraphs 为空")

    total = len(paragraphs)
    results = [None] * total

    def gen():
        nonlocal results  # 多段分支会对 results 重新赋值，声明为外层变量以免单段分支读取时报 UnboundLocalError
        try:
            ai = TriModelAI(novel_id)
        except Exception as e:
            yield "data: " + json.dumps({"type": "done", "error": f"AI 初始化失败: {str(e)}", "results": results}, ensure_ascii=False) + "\n\n"
            return

        # 单段：走流式（保持向后兼容）
        if total <= 1:
            orig_text = (paragraphs[0] or "").strip()
            yield "data: " + json.dumps({
                "type": "progress", "index": 1, "total": 1,
                "paragraph_idx": 0, "percent": 0,
                "original_preview": (orig_text[:40] + "...") if len(orig_text) > 40 else orig_text
            }, ensure_ascii=False) + "\n\n"

            if not orig_text:
                results[0] = ""
                yield "data: " + json.dumps({"type": "result", "paragraph_idx": 0, "final_text": "", "changed": False}, ensure_ascii=False) + "\n\n"
                yield "data: " + json.dumps({"type": "done", "results": results}, ensure_ascii=False) + "\n\n"
                return

            g = ai.writer_rewrite(
                original_text=orig_text, instruction=instruction,
                chapter_idx=chapter_idx, chapter_title=chapter_title,
                global_prompt=global_prompt,
                custom_instruction=custom_instruction, stream=True
            )
            buf = ""
            try:
                for chunk in g:
                    if chunk.get("error"):
                        yield "data: " + json.dumps({"type": "error", "paragraph_idx": 0, "error": chunk["error"]}, ensure_ascii=False) + "\n\n"
                        results[0] = orig_text
                        break
                    c = chunk.get("content") or ""
                    if c:
                        buf += c
                        yield "data: " + json.dumps({"type": "chunk", "paragraph_idx": 0, "content": c}, ensure_ascii=False) + "\n\n"
                    if chunk.get("done"):
                        break
            except GeneratorExit:
                # 客户端断开连接，停止生成
                return
            if results[0] is None:
                results[0] = buf
            yield "data: " + json.dumps({"type": "result", "paragraph_idx": 0, "final_text": results[0] or "", "changed": results[0] != orig_text}, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({"type": "done", "results": results}, ensure_ascii=False) + "\n\n"
            return

        # 多段：统一改写（一次 AI 调用，通读全文）
        try:
            yield "data: " + json.dumps({
                "type": "progress", "total": total, "percent": 5,
                "paragraphs": [(p or "")[:40] for p in paragraphs]
            }, ensure_ascii=False) + "\n\n"

            unified_result = ai.writer_rewrite_unified(
                paragraphs=paragraphs, instruction=instruction,
                chapter_idx=chapter_idx, chapter_title=chapter_title,
                global_prompt=global_prompt,
                custom_instruction=custom_instruction
            )

            if isinstance(unified_result, dict) and "error" in unified_result:
                yield "data: " + json.dumps({"type": "done", "error": unified_result["error"], "results": [p or "" for p in paragraphs]}, ensure_ascii=False) + "\n\n"
                return

            # writer_rewrite_unified 成功时返回 (段落列表, 新章节名或None)
            unified_result, new_title = unified_result

            yield "data: " + json.dumps({"type": "progress", "percent": 50, "total": total}, ensure_ascii=False) + "\n\n"

            # 适配变长结果：AI可能返回不同数量的段落
            result_count = len(unified_result)
            if result_count != total:
                # 扩展或收缩 results 数组
                results = [None] * result_count

            for i, new_text in enumerate(unified_result):
                orig = (paragraphs[i] or "").strip() if i < len(paragraphs) else "(新增段)"
                final = (new_text or "").strip()
                results[i] = final
                yield "data: " + json.dumps({
                    "type": "result", "paragraph_idx": i, "final_text": final,
                    "changed": final != orig,
                    "total_paragraphs": result_count
                }, ensure_ascii=False) + "\n\n"

            yield "data: " + json.dumps({
                "type": "done", "percent": 100, "total": result_count, "results": results,
                "new_title": new_title
            }, ensure_ascii=False) + "\n\n"
        except GeneratorExit:
            return
        except Exception as e:
            yield "data: " + json.dumps({
                "type": "done", "error": f"统一改写异常: {str(e)}", "results": results
            }, ensure_ascii=False) + "\n\n"

    return Response(stream_with_context(gen()), mimetype="text/event-stream")


@app.route("/api/ai/reviewer/check", methods=["POST"])
def ai_reviewer_check():
    data = request.json or {}
    novel_id = data.get("novel_id")
    if not novel_id:
        return jsonify({"error": "缺少novel_id"}), 400
    try:
        ai = TriModelAI(novel_id)
    except Exception as e:
        return _json_error(f"AI 初始化失败: {str(e)}", 500)
    res = ai.reviewer_check(text=data.get("text", ""), chapter_idx=data.get("chapter_idx"))
    return jsonify(res)

@app.route("/api/ai/chat/answer", methods=["POST"])
def ai_chat_answer():
    data = request.json or {}
    novel_id = data.get("novel_id")
    if not novel_id:
        return jsonify({"error": "缺少novel_id"}), 400
    stream = data.get("stream", False)
    if stream:
        def gen():
            try:
                ai = TriModelAI(novel_id)
            except Exception as e:
                yield "data: " + json.dumps({"error": f"AI 初始化失败: {str(e)}", "done": True}, ensure_ascii=False) + "\n\n"
                return
            g = ai.chat_answer(
                question=data.get("question", ""),
                history=data.get("history", []),
                stream=True
            )
            try:
                for chunk in g:
                    yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
            except Exception as e:
                yield "data: " + json.dumps({"error": str(e), "done": True}, ensure_ascii=False) + "\n\n"
        return Response(stream_with_context(gen()), mimetype="text/event-stream")
    try:
        ai = TriModelAI(novel_id)
    except Exception as e:
        return _json_error(f"AI 初始化失败: {str(e)}", 500)
    res = ai.chat_answer(question=data.get("question", ""), history=data.get("history", []))
    return jsonify(res)

@app.route("/api/ai/refs", methods=["POST"])
def ai_memory_refs():
    data = request.json or {}
    novel_id = data.get("novel_id")
    if not novel_id:
        return jsonify({"error": "缺少novel_id"}), 400
    try:
        ai = TriModelAI(novel_id)
    except Exception as e:
        return _json_error(f"AI 初始化失败: {str(e)}", 500)
    res = ai.get_memory_refs(query=data.get("query", ""), top_k=int(data.get("top_k", 5)),
                             chapter_idx=data.get("chapter_idx"))
    return jsonify(res)


# ========== 读书模式（Study/Reading Mode）==========
_study_threads = {}  # novel_id -> {"thread": Thread, "stop": Event, "progress": {...}}

@app.route("/api/novels/<novel_id>/study/start", methods=["POST"])
def api_study_start(novel_id):
    """启动读书模式：AI 逐章阅读并自动提取设定"""
    data = request.json or {}
    chapter_indices = data.get("chapters")  # None=全部, [1,2,3]=指定章节

    # 检查是否已在运行
    if novel_id in _study_threads:
        tinfo = _study_threads[novel_id]
        if tinfo["thread"].is_alive():
            return jsonify({"ok": True, "message": "读书已在运行中", "progress": tinfo["progress"]})

    # 获取章节列表
    from services.novel_service import NovelService
    svc = NovelService()
    toc = svc.get_toc(novel_id)
    if not toc:
        return _json_error("未找到该小说的章节列表")

    all_chapters = sorted(toc, key=lambda c: c["index"])
    if chapter_indices:
        target_chapters = [c for c in all_chapters if c["index"] in chapter_indices]
    else:
        target_chapters = all_chapters

    if not target_chapters:
        return _json_error("未找到指定章节")

    stop_event = threading.Event()
    progress = {"novel_id": novel_id, "total": len(target_chapters),
                "done": 0, "current": 0, "status": "starting",
                "results": [], "summary": None}

    _study_threads[novel_id] = {"thread": None, "stop": stop_event, "progress": progress}

    def _study_worker():
        progress["status"] = "running"
        try:
            ai = TriModelAI(novel_id)
        except Exception as e:
            progress["status"] = "error"
            progress["error"] = f"AI初始化失败: {e}"
            return

        for i, ch in enumerate(target_chapters):
            if stop_event.is_set():
                progress["status"] = "stopped"
                return

            progress["current"] = ch["index"]
            progress["done"] = i

            # 获取完整章节内容（若存在批量修改的待确认内容，优先读修改版）
            full_ch = svc.get_chapter(novel_id, ch["index"])
            content = full_ch.get("content", "") if full_ch else ""
            title = ch.get("title", "")
            pend = _bulk_pending.get(novel_id, {})
            if ch["index"] in pend:
                content = pend[ch["index"]].get("modified", content)

            # 建立向量记忆（确保向量数据库有该章内容，供AI检索引用）
            try:
                ai.memory.index_chapter(ch["index"], title, content)
            except Exception:
                pass

            # AI 提取设定
            res = ai.reviewer_summarize_chapter(
                chapter_idx=ch["index"],
                chapter_title=title,
                chapter_content=content
            )
            progress["results"].append(res)
            progress["done"] = i + 1
            progress["last_chapter"] = ch["index"]
            progress["last_title"] = title

        # 全部完成
        if not stop_event.is_set():
            progress["status"] = "done"
            progress["summary"] = ai.load_settings_summary()

    t = threading.Thread(target=_study_worker, daemon=True)
    _study_threads[novel_id]["thread"] = t
    t.start()

    return jsonify({"ok": True, "message": "读书已启动", "progress": progress})


@app.route("/api/novels/<novel_id>/study/stop", methods=["POST"])
def api_study_stop(novel_id):
    """停止读书"""
    if novel_id in _study_threads:
        _study_threads[novel_id]["stop"].set()
        return jsonify({"ok": True, "message": "已发送停止信号"})
    return jsonify({"ok": False, "message": "没有运行中的读书任务"})


@app.route("/api/novels/<novel_id>/study/status", methods=["GET"])
def api_study_status(novel_id):
    """查询读书进度"""
    if novel_id in _study_threads:
        p = _study_threads[novel_id]["progress"]
        alive = _study_threads[novel_id]["thread"].is_alive()
        return jsonify({"ok": True, "running": alive, "progress": p})
    return jsonify({"ok": True, "running": False, "progress": None})


@app.route("/api/novels/<novel_id>/settings_summary", methods=["GET"])
def api_settings_summary(novel_id):
    """获取已提取的设定摘要（兼容旧接口，从世界书构建）"""
    try:
        ai = TriModelAI(novel_id)
        summary = ai.load_settings_summary()
        return jsonify({"ok": True, "summary": summary})
    except Exception as e:
        return _json_error(f"获取设定摘要失败: {e}")


# ========== 世界书（Worldbook）管理 ==========

@app.route("/api/novels/<novel_id>/worldbook", methods=["GET"])
def api_worldbook_get(novel_id):
    """获取世界书全部条目"""
    try:
        ai = TriModelAI(novel_id)
        return jsonify({"ok": True, "book": ai.load_worldbook()})
    except Exception as e:
        return _json_error(f"获取世界书失败: {e}")


@app.route("/api/novels/<novel_id>/worldbook", methods=["DELETE"])
def api_worldbook_clear(novel_id):
    """清空某本小说的全部世界书条目"""
    try:
        ai = TriModelAI(novel_id)
        book = ai.load_worldbook()
        book["entries"] = []
        book["last_chapter"] = 0
        ai.save_worldbook(book)
        return jsonify({"ok": True, "message": "世界书已清空"})
    except Exception as e:
        return _json_error(f"清空世界书失败: {e}")


@app.route("/api/novels/<novel_id>/worldbook/dedupe", methods=["POST"])
def api_worldbook_dedupe(novel_id):
    """整理去重：把现有世界书中同一实体的重复/细分条目自动合并"""
    try:
        ai = TriModelAI(novel_id)
        book = ai.load_worldbook()
        old_entries = book.get("entries", [])
        if not old_entries:
            return jsonify({"ok": True, "book": book, "removed": 0})
        fresh = {"entries": [], "last_chapter": book.get("last_chapter", 0)}
        for e in old_entries:
            ai._merge_worldbook_entries(fresh, [e], 0)
        fresh["last_chapter"] = book.get("last_chapter", 0)
        ai.save_worldbook(fresh)
        removed = len(old_entries) - len(fresh["entries"])
        return jsonify({"ok": True, "removed": removed, "book": fresh})
    except Exception as e:
        return _json_error(f"整理去重失败: {e}")


@app.route("/api/novels/<novel_id>/worldbook/entry", methods=["PUT"])
def api_worldbook_put(novel_id):
    """新增或更新一个世界书条目（按 id 更新；无 id 则新增）"""
    data = request.json or {}
    entry = data.get("entry") or {}
    name = str(entry.get("name") or "").strip()
    if not name:
        return _json_error("条目名称不能为空")
    try:
        ai = TriModelAI(novel_id)
        book = ai.load_worldbook()
        entries = book.setdefault("entries", [])
        entry["name"] = name
        entry["category"] = str(entry.get("category") or "其他").strip()
        entry["keys"] = [str(k).strip() for k in (entry.get("keys") or []) if str(k).strip()]
        entry["content"] = str(entry.get("content") or "").strip()
        eid = entry.get("id")
        if eid:
            for e in entries:
                if e.get("id") == eid:
                    e.update(entry)
                    break
            else:
                entry["id"] = f"wb_{int(__import__('time').time() * 1000)}"
                entries.append(entry)
        else:
            entry["id"] = f"wb_{int(__import__('time').time() * 1000)}"
            entries.append(entry)
        ai.save_worldbook(book)
        return jsonify({"ok": True, "book": book})
    except Exception as e:
        return _json_error(f"保存条目失败: {e}")


@app.route("/api/novels/<novel_id>/worldbook/entry/<entry_id>", methods=["DELETE"])
def api_worldbook_delete(novel_id, entry_id):
    """删除一个世界书条目"""
    try:
        ai = TriModelAI(novel_id)
        book = ai.load_worldbook()
        entries = book.get("entries", [])
        book["entries"] = [e for e in entries if e.get("id") != entry_id]
        ai.save_worldbook(book)
        return jsonify({"ok": True, "book": book})
    except Exception as e:
        return _json_error(f"删除条目失败: {e}")


@app.route("/api/novels/<novel_id>/worldbook/test", methods=["POST"])
def api_worldbook_test(novel_id):
    """测试关键词触发读取：输入文本，返回会被命中的世界书条目"""
    data = request.json or {}
    text = (data.get("text") or "").strip()
    if not text:
        return _json_error("请输入测试文本")
    try:
        ai = TriModelAI(novel_id)
        book = ai.load_worldbook()
        hit = ai._match_worldbook(book.get("entries", []), text)
        return jsonify({"ok": True, "hit_count": len(hit),
                        "hit": [{
                            "id": e.get("id"), "category": e.get("category"),
                            "name": e.get("name"), "keys": e.get("keys", []),
                            "content": e.get("content", "")
                        } for e in hit]})
    except Exception as e:
        return _json_error(f"测试读取失败: {e}")


@app.route("/api/novels/<novel_id>/worldbook/extract", methods=["POST"])
def api_worldbook_extract(novel_id):
    """手动触发 AI 提取指定章节的设定为世界书条目"""
    data = request.json or {}
    idx = data.get("chapter_idx")
    if idx is None:
        return _json_error("请指定章节号")
    try:
        ch = novel_svc.get_chapter(novel_id, int(idx))
        if not ch:
            return _json_error("章节不存在")
        ai = TriModelAI(novel_id)
        res = ai.reviewer_summarize_chapter(int(idx), ch.get("title", ""),
                                            ch.get("content", ""))
        return jsonify({"ok": True, "result": res})
    except Exception as e:
        return _json_error(f"提取设定失败: {e}")


# ========== 外部 API 管理 ==========
@app.route("/api/external/check", methods=["GET"])
def api_external_check():
    from core.external_client import ExternalAPIClient
    client = ExternalAPIClient()
    return jsonify(client.check_connection())


@app.route("/api/external/models", methods=["GET"])
def api_external_models():
    from core.external_client import ExternalAPIClient
    client = ExternalAPIClient()
    return jsonify(client.list_models())


@app.route("/api/ai/provider", methods=["POST"])
def api_set_ai_provider():
    """切换 AI 供应商（local/external）"""
    data = request.json or {}
    provider = data.get("provider", "local")
    cfg = load_config()
    if "ai_provider" not in cfg:
        cfg["ai_provider"] = {}
    cfg["ai_provider"]["provider"] = provider
    save_config(cfg)
    return jsonify({"ok": True, "provider": provider})


@app.route("/api/config/external", methods=["POST"])
def api_save_external_config():
    """保存外部 API 配置（多槽位）：支持切换/新增/删除槽位，字段保存到当前槽位。"""
    data = request.json or {}
    cfg = load_config()
    if "external_api" not in cfg:
        cfg["external_api"] = {}
    ex = cfg["external_api"]
    # 迁移：无 slots 结构时，把顶层字段收纳为 default 槽位
    if not isinstance(ex.get("slots"), dict) or not ex["slots"]:
        ex["slots"] = {"default": {k: ex.get(k) for k in ("base_url", "api_key", "model", "timeout", "temperature")}}
        ex["active_slot"] = ex.get("active_slot") or "default"
    # 新增槽位（复制当前槽位配置）
    if data.get("add_slot"):
        name = str(data["add_slot"]).strip()
        if not name:
            return _json_error("槽位名不能为空")
        cur = ex.get("active_slot") or "default"
        if name not in ex["slots"]:
            ex["slots"][name] = dict(ex["slots"].get(cur, {}))
        ex["active_slot"] = name
    # 删除槽位
    if data.get("delete_slot"):
        name = str(data["delete_slot"]).strip()
        if name in ex["slots"]:
            if len(ex["slots"]) <= 1:
                return _json_error("至少需要保留一个槽位")
            del ex["slots"][name]
            if ex.get("active_slot") == name:
                ex["active_slot"] = next(iter(ex["slots"]))
    # 切换槽位
    if data.get("active_slot"):
        name = str(data["active_slot"]).strip()
        if name in ex["slots"]:
            ex["active_slot"] = name
    # 保存字段到当前槽位
    active = ex.get("active_slot") or "default"
    slot = ex["slots"].setdefault(active, {})
    for k in ("base_url", "api_key", "model", "timeout", "temperature"):
        if k in data:
            slot[k] = data[k]
    # 同步顶层字段（enabled 与旧读取逻辑兼容；其余与当前槽位保持一致）
    for k in ("base_url", "api_key", "model", "timeout", "temperature"):
        ex[k] = slot.get(k, ex.get(k))
    if "enabled" in data:
        ex["enabled"] = data["enabled"]
    save_config(cfg)
    return jsonify({"ok": True, "active_slot": active, "slots": list(ex["slots"].keys())})


# ========== 文本检索（纯代码检索，非向量） ==========
@app.route("/api/novels/<novel_id>/search", methods=["POST"])
def api_text_search(novel_id):
    data = request.json or {}
    keyword = (data.get("keyword") or "").strip()
    if not keyword:
        return _json_error("缺少搜索关键词")
    start = data.get("start")
    end = data.get("end")
    results = novel_svc.search_text(
        novel_id, keyword,
        start=int(start) if start is not None else None,
        end=int(end) if end is not None else None,
        case_sensitive=bool(data.get("case_sensitive", False))
    )
    return jsonify({"ok": True, "keyword": keyword, "count": len(results), "results": results})


# ========== 文本查找替换（纯文本，支持范围与向量记忆同步） ==========
def _resolve_replace_targets(novel_id, data):
    """解析替换范围，返回 (toc_item列表, 命中次数dict)。"""
    old_text = (data.get("old_text") or "")
    if not old_text:
        return None, None
    start = data.get("start")
    end = data.get("end")
    chapters = data.get("chapters")
    toc = novel_svc.get_toc(novel_id)
    if not toc:
        return [], {}
    chapters_set = None
    if chapters is not None:
        chapters_set = set(int(c) for c in chapters)
    targets = []
    for item in toc:
        idx = item["index"]
        if start is not None and idx < int(start):
            continue
        if end is not None and idx > int(end):
            continue
        if chapters_set is not None and idx not in chapters_set:
            continue
        ch = novel_svc.get_chapter(novel_id, idx)
        if not ch:
            continue
        content = ch.get("modified_content") or ch.get("content") or ""
        targets.append({"index": idx, "title": item.get("title", ""), "content": content})
    return targets, old_text


@app.route("/api/novels/<novel_id>/replace/preview", methods=["POST"])
def api_replace_preview(novel_id):
    """预览替换命中：返回哪些章节会命中、每章命中次数（正文+标题，不实际替换）。"""
    data = request.json or {}
    old_text = (data.get("old_text") or "").strip()
    if not old_text:
        return _json_error("缺少查找文本")
    targets, _ = _resolve_replace_targets(novel_id, data)
    if targets is None:
        return jsonify({"ok": True, "results": [], "total_replacements": 0})
    results = []
    total = 0
    for t in targets:
        cnt = t["content"].count(old_text)
        title_cnt = t["title"].count(old_text)
        if cnt or title_cnt:
            results.append({"index": t["index"], "title": t["title"],
                            "count": cnt, "title_count": title_cnt})
            total += cnt + title_cnt
    return jsonify({"ok": True, "results": results, "total_replacements": total})


@app.route("/api/novels/<novel_id>/replace", methods=["POST"])
def api_replace(novel_id):
    """执行文本替换：old_text -> new_text（正文+标题），替换后更新向量记忆。"""
    data = request.json or {}
    old_text = (data.get("old_text") or "")
    new_text = data.get("new_text") or ""
    if not old_text:
        return _json_error("缺少查找文本")
    targets, _ = _resolve_replace_targets(novel_id, data)
    if targets is None:
        return jsonify({"ok": True, "replaced_chapters": [], "total_replacements": 0})
    replaced_chapters = []
    total = 0
    for t in targets:
        content = t["content"]
        title = t["title"]
        cnt = content.count(old_text)
        title_cnt = title.count(old_text)
        if not cnt and not title_cnt:
            continue
        new_content = content.replace(old_text, new_text)
        new_title = title.replace(old_text, new_text) if title_cnt else title
        novel_svc.save_chapter(novel_id, t["index"], {
            "modified_content": new_content,
            "title": new_title,
        }, auto_mark_title=False)
        # 更新向量记忆（用替换后的标题）
        try:
            mem = ChromaMemory(novel_id)
            mem.index_chapter(t["index"], new_title, new_content)
        except Exception:
            pass
        replaced_chapters.append(t["index"])
        total += cnt + title_cnt
    return jsonify({"ok": True, "replaced_chapters": replaced_chapters,
                    "total_replacements": total})


# ========== 总体设定修改（批量修改 + 待确认 + 历史记录） ==========
_bulk_pending = {}   # novel_id -> {chapter_idx: {"original","modified","original_paragraphs","modified_paragraphs","title","keywords"}}
_bulk_threads = {}   # novel_id -> {"thread","stop","progress"}


def _bulk_history_path(novel_id) -> Path:
    return NOVELS_DIR / novel_id / "bulk_history.json"


def _append_bulk_history(novel_id: str, entry: dict):
    hp = _bulk_history_path(novel_id)
    hist = []
    if hp.exists():
        try:
            hist = json.loads(hp.read_text("utf-8"))
        except Exception:
            hist = []
    hist.append(entry)
    try:
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(json.dumps(hist, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


@app.route("/api/novels/<novel_id>/bulk/start", methods=["POST"])
def api_bulk_start(novel_id):
    data = request.json or {}
    mode = str(data.get("mode") or "keyword").strip().lower() or "keyword"  # keyword=关键词触发 | all=全章节逐章
    keywords = [k.strip() for k in (data.get("keywords") or []) if k and k.strip()]
    instruction = (data.get("instruction") or "").strip()
    chapter_range = data.get("chapter_range") or {}

    if mode == "all":
        # 全章节模式：不依赖关键词，AI 逐章阅读后自行判断是否需要修改
        if not instruction:
            return _json_error("请填写修改要求")
    else:
        if not keywords:
            return _json_error("请至少输入一个关键词")
        if not instruction:
            return _json_error("请填写修改要求")

    toc = novel_svc.get_toc(novel_id)
    if not toc:
        return _json_error("未找到该小说的章节列表")
    all_chapters = sorted(toc, key=lambda c: c["index"])

    # 解析章节范围
    if isinstance(chapter_range, list) and chapter_range:
        indices = set(int(x) for x in chapter_range)
        target = [c for c in all_chapters if c["index"] in indices]
    else:
        target = all_chapters
        start = chapter_range.get("start")
        end = chapter_range.get("end")
        if start is not None:
            target = [c for c in target if c["index"] >= int(start)]
        if end is not None:
            target = [c for c in target if c["index"] <= int(end)]
    if not target:
        return _json_error("章节范围为空")

    # 已有运行中任务
    if novel_id in _bulk_threads and _bulk_threads[novel_id]["thread"].is_alive():
        return jsonify({"ok": True, "message": "批量修改已在运行中", "progress": _bulk_threads[novel_id]["progress"]})

    stop_event = threading.Event()
    progress = {
        "novel_id": novel_id, "status": "starting",
        "total": len(target), "done": 0, "current": 0,
        "searched": 0, "hit": [], "modified": [], "skipped": [], "errors": [],
        "current_keyword": "", "last_chapter": 0, "last_title": "",
        "instruction": instruction, "keywords": keywords, "mode": mode,
        "chapter_range": (chapter_range if not isinstance(chapter_range, list)
                          else {"list": [int(x) for x in chapter_range]})
    }
    _bulk_threads[novel_id] = {"thread": None, "stop": stop_event, "progress": progress}

    g_prompt = data.get("global_prompt") or ""
    c_instruction = data.get("custom_instruction") or ""

    def _worker():
        progress["status"] = "running"
        try:
            ai = TriModelAI(novel_id)
        except Exception as e:
            progress["status"] = "error"
            progress["error"] = f"AI初始化失败: {e}"
            return

        # 带退避重试的 AI 调用：连接类错误（网络波动/风控限流）等待 8 秒后重试
        def _ai_call(method_name, *args, max_attempts=3, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    res = getattr(ai, method_name)(*args, **kwargs)
                except Exception as e:
                    if attempt < max_attempts:
                        progress["errors"].append({"chapter": idx, "error": f"{method_name} 异常({e})，等待后自动重试"})
                        time.sleep(8)
                        continue
                    return {"error": str(e)}
                if isinstance(res, dict) and res.get("error"):
                    err = str(res["error"])
                    is_conn = ("无法连接" in err or "连接失败" in err or "请求超时" in err
                               or "Connection" in err or "timed out" in err.lower())
                    if is_conn and attempt < max_attempts:
                        progress["errors"].append({"chapter": idx, "error": f"{err}（第{attempt}次尝试后自动重试）"})
                        time.sleep(8)
                        continue
                return res
            return res

        # 已修改章节记录：用于跨章节一致性，避免重复冗杂修改
        modified_records = []
        for i, ch in enumerate(target):
            if stop_event.is_set():
                progress["status"] = "stopped"
                return
            idx = ch["index"]
            progress["current"] = idx
            progress["done"] = i
            full_ch = novel_svc.get_chapter(novel_id, idx)
            content = full_ch.get("content", "") if full_ch else ""
            if not content:
                progress["done"] = i + 1
                continue

            # 关键词命中检测（纯文本）
            lower = content.lower()
            if mode == "all":
                # 全章节模式：不按关键词筛选，每章都交给 AI 阅读后自行判断
                found = [kw for kw in keywords if kw.lower() in lower]  # 关键词可选，仅作提示
                progress["searched"] += 1
                progress["hit"].append(idx)
            else:
                found = [kw for kw in keywords if kw.lower() in lower]
                progress["searched"] += 1
                if not found:
                    progress["done"] = i + 1
                    continue
                progress["hit"].append(idx)
            progress["current_keyword"] = ", ".join(found) if found else "（AI 自行判断）"
            title = ch.get("title", "")

            # 阶段1：提取设定（保证设定正确性；连接类错误自动重试一次）
            progress["phase"] = f"reading:{idx}"
            _ai_call("reviewer_summarize_chapter", idx, title, content, max_attempts=2)

            # 阶段2：预思考本章修改方案（连接类错误自动退避重试）
            progress["phase"] = f"planning:{idx}"
            plan = _ai_call("plan_chapter_modification",
                            chapter_idx=idx, chapter_title=title,
                            chapter_content=content, keywords=found,
                            instruction=instruction,
                            global_prompt=g_prompt, custom_instruction=c_instruction)
            progress.setdefault("plans", {})[idx] = plan

            # AI 判断本章无需修改则跳过（不进入修改阶段），省时省 token
            if isinstance(plan, dict) and not plan.get("error"):
                need = plan.get("need_modify", True)
                plan_items = plan.get("plan") or []
                if not need or not plan_items:
                    progress["skipped"].append(idx)
                    progress["done"] = i + 1
                    continue

            # 构建关键词上下文片段
            snippets = []
            for kw in found:
                k = kw.lower()
                pos = 0
                while len(snippets) < 6:
                    p = lower.find(k, pos)
                    if p == -1:
                        break
                    cs = max(0, p - 80)
                    ce = min(len(content), p + len(kw) + 80)
                    snippets.append({"keyword": kw, "context": content[cs:ce]})
                    pos = p + len(kw)
                if len(snippets) >= 6:
                    break

            # 已修改章节回顾（最近5章），供跨章节一致性
            already_ctx = ""
            if modified_records:
                lines = []
                for r in modified_records[-5:]:
                    lines.append(
                        f"第{r['chapter']}章《{r['title']}》（命中关键词：{'、'.join(r['keywords'])}）\n"
                        f"已修改为：{r['snippet']}"
                    )
                already_ctx = "\n\n".join(lines)

            # 阶段3：基于方案执行修改（连接类错误自动退避重试）
            progress["phase"] = f"modifying:{idx}"
            res = _ai_call("bulk_modify_chapter",
                           chapter_idx=idx, chapter_title=title,
                           chapter_content=content, keyword_contexts=snippets,
                           keywords=found, instruction=instruction,
                           global_prompt=g_prompt, custom_instruction=c_instruction,
                           already_modified_context=already_ctx,
                           modification_plan=plan)
            if isinstance(res, dict) and res.get("error"):
                progress["errors"].append({"chapter": idx, "error": res["error"]})
                progress["done"] = i + 1
                continue

            # bulk_modify_chapter 成功时返回 (修改后全文, 新章节名或None)
            new_title = None
            if isinstance(res, tuple):
                modified, new_title = res
            else:
                modified = res if isinstance(res, str) else ""
            if modified and modified.strip() and modified.strip() != content.strip():
                _bulk_pending.setdefault(novel_id, {})[idx] = {
                    "original": content,
                    "modified": modified,
                    "original_paragraphs": full_ch.get("paragraphs", []),
                    "modified_paragraphs": chapter_to_paragraphs(modified),
                    "title": title,
                    "new_title": new_title,
                    "keywords": found
                }
                # 每修改一章立即更新向量记忆，使后续章节检索到已修改内容
                try:
                    ai.memory.index_chapter(idx, title, modified)
                except Exception:
                    pass
                modified_records.append({
                    "chapter": idx, "title": title, "keywords": found,
                    "snippet": modified[:150]
                })
                progress["modified"].append(idx)
            progress["done"] = i + 1
            progress["last_chapter"] = idx
            progress["last_title"] = title
            progress["modified_records"] = modified_records

        if not stop_event.is_set():
            progress["status"] = "done"
            progress["phase"] = "done"

    t = threading.Thread(target=_worker, daemon=True)
    _bulk_threads[novel_id]["thread"] = t
    t.start()
    return jsonify({"ok": True, "message": "批量修改已启动", "progress": progress})


@app.route("/api/novels/<novel_id>/bulk/stop", methods=["POST"])
def api_bulk_stop(novel_id):
    if novel_id in _bulk_threads:
        _bulk_threads[novel_id]["stop"].set()
        return jsonify({"ok": True, "message": "已发送停止信号"})
    return jsonify({"ok": False, "message": "没有运行中的批量修改任务"})


@app.route("/api/novels/<novel_id>/bulk/status", methods=["GET"])
def api_bulk_status(novel_id):
    info = _bulk_threads.get(novel_id)
    pending = _bulk_pending.get(novel_id, {})
    return jsonify({
        "ok": True,
        "running": bool(info and info["thread"].is_alive()),
        "progress": info["progress"] if info else None,
        "pending_count": len(pending),
        "pending_chapters": sorted(pending.keys())
    })


@app.route("/api/novels/<novel_id>/bulk/pending", methods=["GET"])
def api_bulk_pending_list(novel_id):
    pending = _bulk_pending.get(novel_id, {})
    items = [{
        "index": k,
        "title": v.get("title", ""),
        "new_title": v.get("new_title", ""),
        "keywords": v.get("keywords", []),
        "modified_preview": (v.get("modified") or "")[:120],
        "changed": bool((v.get("modified") or "") != (v.get("original") or ""))
    } for k, v in sorted(pending.items())]
    return jsonify({"ok": True, "pending": items})


@app.route("/api/novels/<novel_id>/bulk/confirm", methods=["POST"])
def api_bulk_confirm(novel_id):
    """保留待确认修改。可带 chapters 参数只保留指定章节；不带则保留全部。"""
    data = request.json or {}
    pending = _bulk_pending.get(novel_id, {})
    if not pending:
        return _json_error("没有待确认的修改")

    chapters = data.get("chapters")
    if chapters is not None:
        targets = {int(c) for c in chapters}
        targets = {i for i in targets if i in pending}
        if not targets:
            return _json_error("指定的章节没有待确认的修改")
    else:
        targets = set(pending.keys())
    if not targets:
        return _json_error("没有可保留的修改")

    confirmed = []
    confirmed_pairs = []  # (idx, entry)，供后台异步更新记忆/设定
    for idx in sorted(targets):
        entry = pending[idx]
        confirmed_pairs.append((idx, entry))
        try:
            data = {"modified_content": entry["modified"]}
            # AI 建议的新章节名优先于原标题（仅在改名开关开启时可能生成）
            title = entry.get("new_title") or entry.get("title", "")
            # 自动给已修改章节标题加【已修改】标记，方便在目录中直观识别（已有标记则不重复加）
            if title and "【已修改】" not in title:
                data["title"] = f"{title}【已修改】"
            novel_svc.save_chapter(novel_id, idx, data)
        except Exception:
            continue
        confirmed.append(idx)
        # 从待确认中移除已保留的章节
        del pending[idx]

    # 向量记忆更新与设定摘要提取耗时较长，放后台线程异步执行，保存接口立即返回
    def _async_update_memory(pairs):
        try:
            a = TriModelAI(novel_id)
        except Exception:
            return
        for idx, e in pairs:
            title = e.get("new_title") or e.get("title", "")
            try:
                a.memory.index_chapter(idx, title, e["modified"])
            except Exception:
                pass
            try:
                a.reviewer_summarize_chapter(idx, title, e["modified"])
            except Exception:
                pass
    if confirmed_pairs:
        threading.Thread(target=_async_update_memory, args=(confirmed_pairs,), daemon=True).start()

    # 记录本次确认保留的章节范围
    progress = (_bulk_threads.get(novel_id, {}).get("progress") or {})
    first = min(confirmed) if confirmed else 0
    last = max(confirmed) if confirmed else 0
    _append_bulk_history(novel_id, {
        "time": int(__import__("time").time()),
        "range": f"{first}-{last}" if confirmed else "",
        "chapters": confirmed,
        "keywords": progress.get("keywords", []),
        "instruction": progress.get("instruction", "")
    })
    # 全部确认完毕后清理线程状态
    if not pending:
        _bulk_pending.pop(novel_id, None)
        _bulk_threads.pop(novel_id, None)
    return jsonify({"ok": True, "confirmed": confirmed, "count": len(confirmed),
                    "remaining": sorted(pending.keys())})


@app.route("/api/novels/<novel_id>/bulk/discard", methods=["POST"])
def api_bulk_discard(novel_id):
    """放弃待确认修改。可带 chapters 参数只放弃指定章节；不带则放弃全部。
    放弃时同步将向量记忆恢复为该章原始内容。"""
    data = request.json or {}
    pending = _bulk_pending.get(novel_id, {})
    if not pending:
        return jsonify({"ok": True, "message": "没有待确认的修改"})

    def _restore_memory(entry):
        try:
            mem = ChromaMemory(novel_id)
            mem.index_chapter(int(entry.get("_idx", 0)), entry.get("title", ""),
                              entry.get("original", ""))
        except Exception:
            pass

    chapters = data.get("chapters")
    if chapters is not None:
        targets = {int(c) for c in chapters}
        targets = {i for i in targets if i in pending}
        for idx in targets:
            entry = pending[idx]
            entry["_idx"] = idx
            _restore_memory(entry)
            del pending[idx]
    else:
        for idx, entry in list(pending.items()):
            entry["_idx"] = idx
            _restore_memory(entry)
        pending.clear()

    if not pending:
        _bulk_pending.pop(novel_id, None)
        _bulk_threads.pop(novel_id, None)
    return jsonify({"ok": True, "message": "已放弃所选未确认修改",
                    "remaining": sorted(pending.keys())})


@app.route("/api/novels/<novel_id>/bulk/rework", methods=["POST"])
def api_bulk_rework(novel_id):
    """对待确认列表中的单个章节重新修改（可用新的修改要求）。"""
    data = request.json or {}
    idx = data.get("chapter_idx")
    instruction = (data.get("instruction") or "").strip()
    if idx is None:
        return _json_error("缺少章节号")
    idx = int(idx)
    pending = _bulk_pending.get(novel_id, {})
    if idx not in pending:
        return _json_error("该章节没有待确认的修改")
    if not instruction:
        return _json_error("请填写新的修改要求")
    try:
        ai = TriModelAI(novel_id)
    except Exception as e:
        return _json_error(f"AI初始化失败: {e}", 500)

    entry = pending[idx]
    title = entry.get("title", "")
    keywords = entry.get("keywords", [])
    content = entry.get("original", "")
    # 关键词上下文片段
    lower = content.lower()
    snippets = []
    for kw in keywords:
        k = kw.lower()
        pos = 0
        while len(snippets) < 6:
            p = lower.find(k, pos)
            if p == -1:
                break
            cs = max(0, p - 80)
            ce = min(len(content), p + len(kw) + 80)
            snippets.append({"keyword": kw, "context": content[cs:ce]})
            pos = p + len(kw)
        if len(snippets) >= 6:
            break
    # 参考批量任务的已修改章节回顾
    already_ctx = ""
    prog = _bulk_threads.get(novel_id, {}).get("progress") or {}
    records = prog.get("modified_records") or []
    if records:
        lines = []
        for r in records[-5:]:
            lines.append(f"第{r['chapter']}章《{r['title']}》（命中关键词：{'、'.join(r['keywords'])}）\n已修改为：{r['snippet']}")
        already_ctx = "\n\n".join(lines)

    res = ai.bulk_modify_chapter(
        chapter_idx=idx, chapter_title=title,
        chapter_content=content, keyword_contexts=snippets,
        keywords=keywords, instruction=instruction,
        global_prompt=(data.get("global_prompt") or ""),
        custom_instruction=(data.get("custom_instruction") or ""),
        already_modified_context=already_ctx
    )
    if isinstance(res, dict) and res.get("error"):
        return _json_error(res["error"], 500)
    # bulk_modify_chapter 成功时返回 (修改后全文, 新章节名或None)
    new_title = None
    if isinstance(res, tuple):
        modified, new_title = res
    else:
        modified = res if isinstance(res, str) else ""
    if not modified or not modified.strip():
        return _json_error("AI 返回内容为空", 500)
    entry["modified"] = modified
    entry["modified_paragraphs"] = chapter_to_paragraphs(modified)
    entry["new_title"] = new_title
    entry["rework_instruction"] = instruction
    # 立即更新向量记忆为新的修改内容
    try:
        ai.memory.index_chapter(idx, title, modified)
    except Exception:
        pass
    return jsonify({"ok": True, "chapter": idx, "modified_preview": modified[:150]})


@app.route("/api/novels/<novel_id>/bulk/history", methods=["GET"])
def api_bulk_history(novel_id):
    hp = _bulk_history_path(novel_id)
    if hp.exists():
        try:
            return jsonify({"ok": True, "history": json.loads(hp.read_text("utf-8"))})
        except Exception:
            pass
    return jsonify({"ok": True, "history": []})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug)
