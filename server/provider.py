"""Small explicit Chat Completions / Responses wire adapters.

Provider reasoning and encrypted continuation items stay in request-local memory.
Only ordinary assistant text and function calls leave the adapter for display.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit

import httpx

MAX_RESPONSE_BYTES = 2_000_000
PROTOCOLS = {"chat_completions": "/chat/completions", "responses": "/responses"}


class AgentFailure(Exception):
    """Curated diagnostics only; never contains a URL, key or upstream body."""

    def __init__(self, message, *, response_received=False):
        super().__init__(message)
        self.response_received = response_received


def protocol_name(config):
    value = config.get("protocol", "chat_completions")
    if value not in PROTOCOLS:
        raise AgentFailure("模型接口协议无效，请选择 Chat Completions 或 Responses。")
    return value


def endpoint_url(config):
    raw, model = str(config.get("base_url") or "").strip(), str(config.get("model") or "").strip()
    if not raw or not model:
        raise AgentFailure("还没有配置模型。请在设置中填写服务地址和模型名称。")
    protocol = protocol_name(config)
    try:
        parts = urlsplit(raw)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment):
            raise ValueError()
        _ = parts.port
        path = parts.path.rstrip("/") or "/v1"
        known = next((kind for kind, suffix in PROTOCOLS.items() if path.endswith(suffix)), None)
        if known and known != protocol:
            raise AgentFailure("完整接口地址与所选协议不一致，请切换协议或改用基础地址。不会自动切换接口。")
        if not known:
            path += PROTOCOLS[protocol]
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    except ValueError:
        raise AgentFailure("模型服务地址无效。请使用 http/https 地址，并将密钥填入独立密钥栏。") from None


def assistant_context(message):
    """Return a memory-only continuation message; never persist this dictionary."""
    result = {"role": "assistant", "content": message.get("content")}
    if message.get("tool_calls"):
        result["tool_calls"] = message["tool_calls"]
    for field in ("_response_output", "_chat_reasoning"):
        if field in message:
            result[field] = message[field]
    return result


def responses_input(messages):
    result = []
    for message in messages:
        if "_response_output" in message:
            # Returning every output item is necessary for stateless reasoning
            # models, including encrypted reasoning items and function calls.
            result.extend(message["_response_output"])
        elif message.get("role") == "tool":
            result.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                           "output": message.get("content") or ""})
        elif message.get("role") == "assistant" and message.get("tool_calls"):
            if message.get("content"):
                result.append({"role": "assistant", "content": message["content"]})
            for call in message["tool_calls"]:
                result.append({"type": "function_call", "call_id": call["id"],
                               "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
        else:
            result.append({"role": message["role"], "content": message.get("content") or ""})
    return result


def make_payload(config, messages, tools=None, tool_choice="auto"):
    protocol = protocol_name(config)
    limit = max(128, min(16000, int(config.get("max_output_tokens", 3000))))
    payload = {"model": config["model"].strip(), "stream": False}
    if config.get("send_temperature", False):
        payload["temperature"] = float(config.get("temperature", 0.2))
    if protocol == "responses":
        payload.update(input=responses_input(messages), store=False, max_output_tokens=limit,
                       include=["reasoning.encrypted_content"])
        if tools:
            # Explicit strict=false retains optional parameters from our schema.
            payload["tools"] = [{"type": "function", **tool["function"], "strict": False} for tool in tools]
            payload["tool_choice"] = ({"type": "function", "name": tool_choice["function"]["name"]}
                                      if isinstance(tool_choice, dict) else tool_choice)
    else:
        wire = []
        for message in messages:
            entry = {key: value for key, value in message.items() if not key.startswith("_")}
            if "_chat_reasoning" in message:
                entry["reasoning_content"] = message["_chat_reasoning"]
            wire.append(entry)
        payload["messages"] = wire
        if "deepseek" in payload["model"].lower():
            payload["thinking"] = {"type": "enabled" if config.get("thinking_enabled", False) else "disabled"}
        reasoning = bool(re.match(r"^(o[134](?:-|$)|gpt-[5-9](?:[.-]|$))", payload["model"], re.I))
        payload["max_completion_tokens" if reasoning else "max_tokens"] = limit
        if tools:
            payload.update(tools=tools, tool_choice=tool_choice)
    return payload


def response_message(data, protocol):
    if not isinstance(data, dict):
        raise ValueError()
    if protocol == "responses":
        if data.get("status") == "incomplete":
            raise AgentFailure("模型输出达到令牌限制，未完成回答。请提高最大输出令牌或缩小任务范围。", response_received=True)
        if data.get("status") in {"failed", "cancelled"} or data.get("error"):
            raise AgentFailure("模型服务未完成本次生成，请检查服务状态后重试。", response_received=True)
        output = data["output"]
        if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
            raise ValueError()
        content, calls = [], []
        for item in output:
            if item.get("type") == "message" and item.get("role") == "assistant":
                for part in item.get("content", []):
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        content.append(part["text"])
                    elif part.get("type") == "refusal":
                        content.append("模型未接受本次请求，请调整问题范围后重试。")
            elif item.get("type") == "function_call":
                calls.append({"type": "function", "id": item["call_id"],
                              "function": {"name": item["name"], "arguments": item["arguments"]}})
        result = {"role": "assistant", "content": "\n".join(content) or None, "_response_output": output}
        if calls:
            result["tool_calls"] = calls
    else:
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise AgentFailure("模型输出达到令牌限制，未完成回答。请提高最大输出令牌或缩小任务范围。", response_received=True)
        raw = choice["message"]
        if not isinstance(raw, dict):
            raise ValueError()
        result = {"role": "assistant", "content": raw.get("content")}
        if raw.get("tool_calls") is not None:
            result["tool_calls"] = raw["tool_calls"]
        if isinstance(raw.get("reasoning_content"), str):
            result["_chat_reasoning"] = raw["reasoning_content"]
    calls, content = result.get("tool_calls"), result.get("content")
    if calls is not None and not isinstance(calls, list):
        raise ValueError()
    if content is not None and not isinstance(content, str):
        raise ValueError()
    if not calls and not (content and content.strip()):
        raise ValueError()
    return result


def http_failure(status, body):
    """Extract only allowlisted categories/parameter names, never raw messages."""
    message = {
        400: "请求参数或接口协议不被服务接受", 401: "密钥无效或认证方式不匹配",
        403: "服务拒绝访问，请检查模型权限或地区访问限制", 404: "接口路径或模型不存在",
        408: "服务处理超时", 422: "请求参数未通过服务校验", 429: "请求频率或额度达到限制",
    }.get(status, "模型服务暂时不可用" if status >= 500 else "模型请求失败")
    if 300 <= status < 400:
        message = "接口发生重定向，请填写服务实际的接口地址"
    try:
        error = json.loads(body).get("error", {})
        if isinstance(error, dict):
            code = error.get("code") or error.get("type")
            categories = {"model_not_found": "模型不存在或无访问权限", "invalid_api_key": "密钥无效",
                          "insufficient_quota": "账户额度不足", "unsupported_parameter": "模型不支持请求中的参数",
                          "unsupported_value": "模型不接受该参数值", "context_length_exceeded": "输入超过模型上下文限制",
                          "rate_limit_exceeded": "请求频率达到限制"}
            if code in categories:
                message = categories[code]
            parameter = error.get("param")
            known = {"temperature", "max_tokens", "max_completion_tokens", "max_output_tokens", "tools",
                     "tool_choice", "messages", "input", "model", "include", "store", "reasoning"}
            if parameter in known:
                message += f"（参数：{parameter}）"
    except (ValueError, AttributeError, TypeError):
        pass
    return AgentFailure(f"{message}，HTTP {status}。请核对设置；系统不会自动切换协议。", response_received=True)


async def request_completion(config, messages, *, tools=None, tool_choice="auto", timeout=None):
    endpoint = endpoint_url(config)
    seconds = float(timeout if timeout is not None else config.get("timeout_seconds", 45))
    payload = make_payload(config, messages, tools, tool_choice)
    headers = {"Content-Type": "application/json", "User-Agent": "Touchline/2.0"}
    session = config.get("_session_id")
    if isinstance(session, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,120}", session):
        headers["x-opencode-session"] = session
    if config.get("api_key"):
        headers["Authorization"] = "Bearer " + config["api_key"]
    try:
        async with httpx.AsyncClient(timeout=seconds, follow_redirects=False,
                                     trust_env=bool(config.get("use_env_proxy", True))) as client:
            async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise AgentFailure("模型返回内容超过大小限制，任务已停止。", response_received=True)
                    chunks.append(chunk)
                body = b"".join(chunks)
                if response.status_code >= 400 or response.is_redirect:
                    raise http_failure(response.status_code, body)
                try:
                    return response_message(json.loads(body), protocol_name(config))
                except (ValueError, KeyError, IndexError, TypeError, UnicodeError, AttributeError):
                    raise AgentFailure("收到服务响应，但格式不符合所选接口。请检查协议及模型的工具调用支持。", response_received=True) from None
    except httpx.TimeoutException:
        raise AgentFailure("模型请求超时，请重试或调整设置中的请求超时。") from None
    except httpx.ProxyError:
        raise AgentFailure("系统代理连接失败。请检查代理，或在设置中关闭使用环境代理。") from None
    except httpx.HTTPError:
        raise AgentFailure("无法连接模型服务，请检查地址、网络、环境代理和本地模型是否已启动。") from None
