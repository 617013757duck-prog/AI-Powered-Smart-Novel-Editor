from __future__ import annotations

import json
import time
import requests
from typing import Optional, Union, Generator

from config.settings import load_config


def _log_external_error(e: Exception, where: str):
    """打印外部 API 连接/请求失败的详细原因，便于排查网络/风控问题。"""
    import sys
    try:
        detail = repr(e)
        # requests 的 ConnectionError 通常携带底层 OSError，提取其 message 更直观
        args = getattr(e, "args", None)
        if args and hasattr(args[0], "reason"):
            reason = args[0].reason
            detail = f"{detail} | reason={getattr(reason, 'reason', reason)!r}"
        print(f"[external_api][{where}] {detail}", file=sys.stderr, flush=True)
    except Exception:
        pass


class ExternalAPIClient:
    """兼容 OpenAI / Claude / DeepSeek-API 等外部 API 的客户端。
    实现与 OllamaClient 相同的 chat/generate/check_connection 接口。
    """

    def __init__(self):
        cfg = load_config().get("external_api", {})
        self.active_slot = cfg.get("active_slot") or "default"
        self.base_url = self._cfg_value(cfg, "base_url", "https://api.openai.com/v1")
        self.api_key = self._cfg_value(cfg, "api_key") or os.getenv("EXTERNAL_API_KEY", "")
        self.model = self._cfg_value(cfg, "model") or "gpt-4o-mini"
        self.timeout = self._cfg_value(cfg, "timeout") or 120
        self.default_temp = self._cfg_value(cfg, "temperature") or 0.7
        self.enabled = bool(cfg.get("enabled") and self.api_key)

    def _cfg_value(self, ex: dict, key: str, default=None):
        """读取当前生效配置：优先 slots.<active_slot>，缺失字段回退顶层（兼容旧版单槽位配置）。"""
        active = ex.get("active_slot") or "default"
        slot = (ex.get("slots") or {}).get(active) or {}
        v = slot.get(key)
        if v in (None, ""):
            v = ex.get(key)
        return v if v not in (None, "") else default

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    def _refresh_config(self):
        cfg = load_config().get("external_api", {})
        self.active_slot = cfg.get("active_slot") or "default"
        self.base_url = self._cfg_value(cfg, "base_url", "https://api.openai.com/v1")
        self.api_key = self._cfg_value(cfg, "api_key") or os.getenv("EXTERNAL_API_KEY", "")
        self.model = self._cfg_value(cfg, "model") or "gpt-4o-mini"
        self.timeout = self._cfg_value(cfg, "timeout") or 120
        self.default_temp = self._cfg_value(cfg, "temperature") or 0.7
        self.enabled = bool(cfg.get("enabled") and self.api_key)

    def chat(self, messages: list, temperature: Optional[float] = None,
             stream: bool = False) -> Optional[Union[dict, Generator]]:
        self._refresh_config()
        if not self.enabled:
            return {"error": "外部API未启用，请在设置中配置API密钥。", "done": True}

        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.default_temp,
            "stream": stream
        }
        try:
            if stream:
                return self._stream(url, payload)
            resp = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {"content": content, "done": True}
        except requests.ConnectionError as e:
            _log_external_error(e, "chat")
            return {"error": f"无法连接到外部API（{self.base_url}），请检查网络与API地址。", "done": True}
        except requests.Timeout:
            return {"error": "外部API请求超时。", "done": True}
        except Exception as e:
            msg = str(e)
            if "401" in msg:
                return {"error": "API密钥无效(401)，请检查设置。", "done": True}
            if "429" in msg:
                return {"error": "API请求频率过高(429)，请稍后重试。", "done": True}
            return {"error": f"外部API出错: {msg}", "done": True}

    def _stream(self, url: str, payload: dict) -> Generator:
        try:
            with requests.post(url, json=payload, headers=self._headers(),
                               timeout=self.timeout, stream=True) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        yield {"content": "", "done": True}
                        break
                    try:
                        obj = json.loads(data_str)
                        delta = obj.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        yield {"content": content, "done": False}
                    except json.JSONDecodeError:
                        continue
        except requests.ConnectionError as e:
            _log_external_error(e, "stream")
            yield {"content": "", "error": f"无法连接到外部API（{self.base_url}）", "done": True}
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
        if not self.enabled:
            return {"ok": False, "type": "external", "active_slot": self.active_slot,
                    "error": "外部API未启用或未配置密钥"}
        try:
            t0 = time.time()
            resp = requests.get(
                f"{self.base_url}/models",
                headers=self._headers(),
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            model_names = [m.get("id", "") for m in data.get("data", [])]
            return {
                "ok": True,
                "type": "external",
                "active_slot": self.active_slot,
                "latency_ms": int((time.time() - t0) * 1000),
                "models": model_names[:20],
                "target_model": self.model,
                "model_available": any(self.model in n for n in model_names)
            }
        except requests.ConnectionError:
            return {"ok": False, "type": "external", "active_slot": self.active_slot,
                    "error": "无法连接外部API端点"}
        except requests.Timeout:
            return {"ok": False, "type": "external", "active_slot": self.active_slot,
                    "error": "连接超时"}
        except Exception as e:
            msg = str(e)
            if "401" in msg:
                return {"ok": False, "type": "external", "active_slot": self.active_slot,
                        "error": "API密钥无效(401)"}
            if "404" in msg:
                # 端点不支持 /models（如火山方舟 Agent Plan），降级用 chat/completions 验证
                return self._verify_by_chat()
            return {"ok": False, "type": "external", "active_slot": self.active_slot,
                    "error": f"连接失败: {msg}"}

    def _verify_by_chat(self) -> dict:
        """通过最小 chat/completions 请求验证连接（用于不支持 /models 的端点）。"""
        try:
            t0 = time.time()
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1
                },
                headers=self._headers(),
                timeout=15
            )
            resp.raise_for_status()
            return {
                "ok": True,
                "type": "external",
                "active_slot": self.active_slot,
                "latency_ms": int((time.time() - t0) * 1000),
                "models": [self.model],
                "target_model": self.model,
                "model_available": True,
                "note": "该端点不支持 /models，已通过 chat/completions 验证"
            }
        except requests.ConnectionError:
            return {"ok": False, "type": "external", "active_slot": self.active_slot,
                    "error": "无法连接外部API端点"}
        except requests.Timeout:
            return {"ok": False, "type": "external", "active_slot": self.active_slot,
                    "error": "连接超时"}
        except Exception as e:
            msg = str(e)
            if "401" in msg:
                return {"ok": False, "type": "external", "active_slot": self.active_slot,
                        "error": "API密钥无效(401)"}
            if "429" in msg:
                return {"ok": False, "type": "external", "active_slot": self.active_slot,
                        "error": "API请求频率过高/配额已用尽(429)"}
            if "404" in msg:
                return {"ok": False, "type": "external", "active_slot": self.active_slot,
                        "error": "模型不可用(404)，请检查模型名"}
            return {"ok": False, "type": "external", "active_slot": self.active_slot,
                    "error": f"连接失败: {msg}"}

    def list_models(self) -> dict:
        self._refresh_config()
        if not self.enabled:
            return {"ok": False, "error": "外部API未启用", "models": []}
        try:
            t0 = time.time()
            resp = requests.get(
                f"{self.base_url}/models",
                headers=self._headers(),
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            models = [{"name": m.get("id", ""), "owned_by": m.get("owned_by", "")}
                      for m in data.get("data", [])]
            return {
                "ok": True,
                "type": "external",
                "active_slot": self.active_slot,
                "latency_ms": int((time.time() - t0) * 1000),
                "models": models,
                "base_url": self.base_url,
                "current_model": self.model
            }
        except Exception as e:
            msg = str(e)
            if "404" in msg:
                # 不支持 /models 的端点：无法枚举，返回当前模型
                return {
                    "ok": True,
                    "type": "external",
                    "active_slot": self.active_slot,
                    "models": [{"name": self.model, "owned_by": "unknown"}],
                    "base_url": self.base_url,
                    "current_model": self.model,
                    "note": "该端点不支持 /models，仅列出当前配置模型"
                }
            return {"ok": False, "type": "external", "active_slot": self.active_slot,
                    "error": str(e), "models": []}


import os
