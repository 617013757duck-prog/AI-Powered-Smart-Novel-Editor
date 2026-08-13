from __future__ import annotations

import json
import time
import requests
from typing import Optional, Union, Generator

from config.settings import load_config


class OllamaClient:
    def __init__(self):
        cfg = load_config()
        self.base_url = cfg["ollama"]["base_url"].rstrip("/")
        self.model = cfg["ollama"]["model"]
        self.timeout = cfg["ollama"]["timeout"]
        self.default_temp = cfg["ollama"]["temperature"]

    def _refresh_config(self):
        cfg = load_config()
        self.base_url = cfg["ollama"]["base_url"].rstrip("/")
        self.model = cfg["ollama"]["model"]
        self.timeout = cfg["ollama"]["timeout"]

    def _resolve_model_name(self) -> str:
        """自动将用户输入的简写模型名解析为 Ollama 实际注册的全名。
        例如 "deepseek" -> "deepseek-r1:7b" """
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=6)
            resp.raise_for_status()
            available = [m.get("name") for m in resp.json().get("models", []) if m.get("name")]
            # 精确匹配优先
            if self.model in available:
                return self.model
            # 子串匹配（例如 deepseek 出现在 deepseek-r1:7b）
            for name in available:
                if self.model in name:
                    return name
            # 反向匹配（例如用户输入 deepseek-r1:7b，但列表中是 deepseek）
            for name in available:
                if name in self.model:
                    return name
        except Exception:
            pass
        return self.model  # 降级返回原值

    def _effective_model(self) -> str:
        """返回解析后的实际模型名，优先从缓存获取"""
        if not hasattr(self, "_cached_model_name"):
            self._cached_model_name = self._resolve_model_name()
        return self._cached_model_name

    def chat(self, messages: list, temperature: Optional[float] = None,
             stream: bool = False) -> Optional[Union[dict, Generator]]:
        self._refresh_config()
        model = self._effective_model()
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "options": {
                "temperature": temperature if temperature is not None else self.default_temp
            }
        }
        try:
            if stream:
                return self._stream_chat(url, payload)
            resp = requests.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            return {"content": data.get("message", {}).get("content", ""), "done": True}
        except requests.ConnectionError:
            return {"error": "无法连接到Ollama服务，请确认Ollama已启动并运行在正确地址。", "done": True}
        except requests.Timeout:
            return {"error": "请求超时，请检查Ollama服务状态或增加超时时间。", "done": True}
        except Exception as e:
            return {"error": f"调用Ollama出错: {str(e)}", "done": True}

    def _stream_chat(self, url: str, payload: dict) -> Generator:
        try:
            with requests.post(url, json=payload, timeout=self.timeout, stream=True) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        chunk = obj.get("message", {}).get("content", "")
                        done = obj.get("done", False)
                        yield {"content": chunk, "done": done}
                    except json.JSONDecodeError:
                        continue
        except requests.ConnectionError:
            yield {"content": "", "error": "无法连接到Ollama服务", "done": True}
        except requests.Timeout:
            yield {"content": "", "error": "请求超时", "done": True}
        except Exception as e:
            yield {"content": "", "error": f"流式出错: {str(e)}", "done": True}

    def generate(self, prompt: str, system: str = "", temperature: Optional[float] = None,
                 stream: bool = False):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, temperature=temperature, stream=stream)

    def check_connection(self) -> dict:
        self._refresh_config()
        try:
            t0 = time.time()
            resp = requests.get(f"{self.base_url}/api/tags", timeout=10)
            resp.raise_for_status()
            tags = resp.json().get("models", [])
            model_names = [m.get("name") for m in tags]
            return {
                "ok": True,
                "latency_ms": int((time.time() - t0) * 1000),
                "models": model_names,
                "target_model": self.model,
                "model_available": any(self.model in n for n in model_names)
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "models": [], "target_model": self.model, "model_available": False}

    def list_models(self) -> dict:
        """列出 Ollama 本地所有已下载模型（供下拉选择切换）"""
        self._refresh_config()
        try:
            t0 = time.time()
            resp = requests.get(f"{self.base_url}/api/tags", timeout=10)
            resp.raise_for_status()
            tags = resp.json().get("models", [])
            models = []
            for m in tags:
                models.append({
                    "name": m.get("name"),
                    "model": m.get("model"),
                    "size": m.get("size", 0),
                    "modified_at": m.get("modified_at"),
                    "details": m.get("details", {})
                })
            return {
                "ok": True,
                "latency_ms": int((time.time() - t0) * 1000),
                "models": models,
                "base_url": self.base_url,
                "current_model": self.model
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "models": [], "current_model": self.model}

    def healthz_gpu(self) -> dict:
        """
        GPU 心跳探测：真实调用一次 /api/chat（non-stream）验证 AI 在跑。
        若返回 ok==True 且 eval_duration>0，则说明 GPU/CPU 推理已生效。
        """
        self._refresh_config()
        if hasattr(self, "_cached_model_name"):
            del self._cached_model_name
        check = self.check_connection()
        if not check["ok"]:
            return {"ok": False, "stage": "connect", **check}
        if not check["model_available"]:
            return {"ok": False, "stage": "model_missing",
                    "error": f"模型 {self.model} 未下载，本地已有：{check['models']}",
                    "models": check["models"]}
        resolved_model = self._effective_model()
        try:
            t0 = time.time()
            payload = {
                "model": resolved_model,
                "messages": [{"role": "user", "content": "OK"}],
                "stream": False,
                "options": {"temperature": 0.0}
            }
            resp = requests.post(f"{self.base_url}/api/chat",
                                 json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            total_ms = int((time.time() - t0) * 1000)
            content = data.get("message", {}).get("content", "")
            eval_count = data.get("eval_count", 0)
            eval_dur = data.get("eval_duration", 0)
            load_dur = data.get("load_duration", 0)
            total_dur = data.get("total_duration", 0)
            ok = bool(content.strip() and eval_count > 0)
            return {
                "ok": ok,
                "stage": "generate",
                "total_ms": total_ms,
                "output_chars": len(content),
                "model": resolved_model,
                "eval_count": eval_count,
                "eval_duration_ms": eval_dur // 1_000_000 if eval_dur else 0,
                "load_duration_ms": load_dur // 1_000_000 if load_dur else 0,
                "total_duration_ms": total_dur // 1_000_000 if total_dur else 0,
                "preview": content[:80],
                "hint": (
                    "✅ AI 已实际运行！GPU/CPU 已被调用。"
                    f"模型加载 {data.get('load_duration', 0) // 1_000_000}ms，"
                    f"推理 {eval_dur // 1_000_000 if eval_dur else '?'}ms。"
                    "若任务管理器仍看不到 GPU 占用，可能是该模型使用 CPU 推理或 Ollama GPU 层数设为 0。"
                ) if ok else (
                    "⚠️ 连接成功但生成内容为空。请检查本地 Ollama 日志或重新 pull 模型。"
                )
            }
        except requests.ConnectionError:
            return {"ok": False, "stage": "generate", "error": "连接失败：Ollama 服务未启动或地址错误"}
        except requests.Timeout:
            return {"ok": False, "stage": "generate", "error": "生成超时（60s）：模型可能仍在加载到 GPU"}
        except Exception as e:
            return {"ok": False, "stage": "generate", "error": f"GPU 心跳异常：{str(e)}"}
