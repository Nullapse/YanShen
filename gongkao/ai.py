import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class AiConfigError(Exception):
    pass


class AiRequestError(Exception):
    pass


def masked_key(value):
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def resolve_api_key(settings):
    inline_key = (settings["api_key"] or "").strip()
    if inline_key:
        return inline_key
    env_name = (settings["api_key_env"] or "").strip()
    if env_name:
        return os.environ.get(env_name, "").strip()
    return ""


def build_chat_url(base_url):
    base = base_url.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    parsed = urlparse(base)
    if parsed.netloc.endswith("api.deepseek.com"):
        return base + "/chat/completions"
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def chat_completion(settings, prompt, request_options=None):
    api_key = resolve_api_key(settings)
    if not api_key:
        raise AiConfigError("未找到 API key。请在设置页填写 API key，或设置对应环境变量。")

    base_url = (settings["api_base_url"] or "").strip().rstrip("/")
    if not base_url:
        raise AiConfigError("API Base URL 不能为空。")
    url = build_chat_url(base_url)
    model = (settings["model"] or "").strip()
    if not model:
        raise AiConfigError("模型名不能为空。")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是专业、严格、可操作的申论批改老师。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": float(settings["temperature"] or 0.2),
    }
    request_options = request_options or {}
    thinking_type = request_options.get("thinking")
    api_host = (urlparse(base_url).hostname or "").lower()
    if (
        thinking_type in {"enabled", "disabled"}
        and api_host == "api.deepseek.com"
        and model.startswith("deepseek-v4")
    ):
        payload["thinking"] = {"type": thinking_type}
    response_format = request_options.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") == "json_object":
        payload["response_format"] = {"type": "json_object"}
        payload["messages"][0]["content"] += (
            " 当前任务要求 JSON 输出：最终内容必须是单个合法 JSON 对象，"
            "不要输出 Markdown、XML 标签或任何额外文字。"
        )
    max_tokens = request_options.get("max_tokens")
    if isinstance(max_tokens, int) and 1 <= max_tokens <= 384000:
        payload["max_tokens"] = max_tokens
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AiRequestError(f"API 请求失败：HTTP {exc.code}。{detail[:500]}") from exc
    except URLError as exc:
        raise AiRequestError(f"API 连接失败：{exc.reason}") from exc
    except TimeoutError as exc:
        raise AiRequestError("API 请求超时，请稍后重试或换用 Codex 手动模式。") from exc

    try:
        parsed = json.loads(raw)
        content = parsed["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise AiRequestError("API 返回格式无法解析，请检查服务商是否兼容 OpenAI chat completions。") from exc
    return content, raw
