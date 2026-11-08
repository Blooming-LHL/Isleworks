"""Small, dependency-free configuration loading for DeepSeek.

The optional ``.env`` file supports UTF-8 (including a BOM), blank lines,
full-line ``#`` comments, and ``KEY=VALUE`` assignments. A simple ``export``
prefix is accepted. Values may use matching single or double quotes; values
are otherwise kept literally (no expansion or execution). Only the three
DeepSeek keys are recognized. Environment values take precedence, including
empty strings.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-flash"
_KEYS = frozenset({"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_ENDPOINT", "DEEPSEEK_MODEL"})


def resolve_chat_endpoint(base_url: str) -> str:
    """Resolve a DeepSeek base URL or legacy endpoint to the chat endpoint."""
    if not isinstance(base_url, str) or not base_url:
        raise ValueError("Invalid model endpoint")
    url = urllib.parse.urlparse(base_url)
    try:
        hostname = url.hostname
    except ValueError:
        hostname = None
    if (url.username or url.password or url.fragment or url.query or not hostname
            or url.path is None):
        raise ValueError("Invalid model endpoint")
    if url.scheme != "https" and not (url.scheme == "http" and hostname in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("Only HTTPS or exact loopback HTTP model endpoints are allowed")
    path = url.path.rstrip("/")
    suffix = "/chat/completions"
    if not path.endswith(suffix):
        path += suffix
    return urllib.parse.urlunparse((url.scheme, url.netloc, path, "", "", ""))


def _parse_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return values

    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export") and (len(line) == 6 or line[6].isspace()):
            line = line[6:].lstrip()
        if not line:
            continue

        if "=" not in line:
            if line.split(None, 1)[0] in _KEYS:
                raise ValueError(f"Malformed configuration line {line_number}")
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in _KEYS:
            continue
        if not value:
            values[key] = value
            continue
        if value[0] in "'\"":
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise ValueError(f"Malformed configuration line {line_number}")
            value = value[1:-1]
        elif value[-1] in "'\"":
            raise ValueError(f"Malformed configuration line {line_number}")
        values[key] = value
    return values


def deepseek_settings(
    path: str | Path = ".env", environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return DeepSeek settings from an optional file and environment."""
    file_values = _parse_file(Path(path))
    env = os.environ if environ is None else environ
    defaults = {
        "DEEPSEEK_API_KEY": "",
        "DEEPSEEK_MODEL": _DEFAULT_MODEL,
    }
    settings = {
        key: env[key] if key in env else file_values.get(key, default)
        for key, default in defaults.items()
    }
    settings["DEEPSEEK_BASE_URL"] = (
        env["DEEPSEEK_BASE_URL"] if "DEEPSEEK_BASE_URL" in env
        else env["DEEPSEEK_ENDPOINT"] if "DEEPSEEK_ENDPOINT" in env
        else file_values.get("DEEPSEEK_BASE_URL", file_values.get("DEEPSEEK_ENDPOINT", _DEFAULT_BASE_URL))
    )
    return settings


class ReviewConfigurationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def configured_review_gateway(*, endpoint: str | None = None, model: str | None = None):
    """Runtime-only credential loading; review has its own two-attempt budget."""
    from isleworks.llm import ModelGateway
    settings = deepseek_settings()
    if not settings["DEEPSEEK_API_KEY"]:
        raise ReviewConfigurationError("missing_api_key", "未配置 DEEPSEEK_API_KEY，无法自动生成模型复盘；事实报告已保留。")
    url = resolve_chat_endpoint(endpoint if endpoint is not None else settings["DEEPSEEK_BASE_URL"])
    return ModelGateway(url, model if model is not None else settings["DEEPSEEK_MODEL"],
                        settings["DEEPSEEK_API_KEY"], max_calls=2, output_tokens=3000,
                        thinking=False, decision_mode="json_object")
