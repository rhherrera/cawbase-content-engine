"""Client of the inference service contract (llama.cpp `llama-server`, OpenAI-compatible).

Contract used by A09-1 (see docs/inference-deployment.md):

- `GET /health`: 503 while the weights load, 200 `{"status": "ok"}` once the model is loaded. No auth, no decoding.
- `GET /v1/models`: lists the configured alias.
- `GET /props`: `default_generation_settings.n_ctx` is the effective context per slot; `total_slots` the slots.
- `POST /apply-template` + `POST /tokenize`: exact prompt token count with the model's own chat template.
- `POST /v1/chat/completions` with `response_format = {"type": "json_schema", "json_schema": {"name", "strict": true,
  "schema"}}`, temperature 0: the structured output is `choices[0].message.content` (reasoning goes to
  `reasoning_content` with `--reasoning-format deepseek`). A prompt larger than the slot context is rejected by the
  server with HTTP 400 `exceed_context_size_error`.

Error classes are closed: `TransientInferenceError` (retry later), `ContextExceededError` and
`OutputInvalidError` (do not retry). Messages never carry prompt or output content.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .decide import Readiness

CANARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
CANARY_MESSAGES = [{"role": "user", "content": 'Reply with the JSON object {"ok": true} and nothing else.'}]


class InferenceError(Exception):
    code = "inference_error"

    def __init__(self, code: str | None = None, **detail: Any) -> None:
        self.code = code or self.code
        self.detail = detail
        super().__init__(self.code)


class TransientInferenceError(InferenceError):
    code = "inference_unavailable"


class ContextExceededError(InferenceError):
    code = "input_exceeds_context"


class OutputInvalidError(InferenceError):
    code = "output_invalid"


def read_api_key(path: str | None) -> str | None:
    if not path:
        return None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    raise ValueError("api key file has no key")


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: Any


class LlamaClient:
    def __init__(self, base_url: str, *, api_key: str | None = None, timeout_s: float = 180.0) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be http(s)")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s

    # --- transport ---------------------------------------------------------------------------------------------

    def request(self, method: str, path: str, body: Any = None, *, timeout_s: float | None = None) -> HttpResult:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        except TimeoutError as exc:
            raise TransientInferenceError("inference_timeout") from exc
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            raise TransientInferenceError("inference_unreachable") from exc
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return HttpResult(status, parsed)

    # --- contract ----------------------------------------------------------------------------------------------

    def health(self, timeout_s: float = 5.0) -> HttpResult:
        return self.request("GET", "/health", timeout_s=timeout_s)

    def model_ids(self, timeout_s: float = 10.0) -> list[str]:
        res = self.request("GET", "/v1/models", timeout_s=timeout_s)
        if res.status != 200 or not isinstance(res.body, dict):
            raise TransientInferenceError(f"models_http_{res.status}")
        return [str(m.get("id")) for m in res.body.get("data", []) if isinstance(m, dict)]

    def slot_context(self) -> int:
        res = self.request("GET", "/props", timeout_s=10.0)
        try:
            return int(res.body["default_generation_settings"]["n_ctx"])
        except (TypeError, KeyError, ValueError) as exc:
            raise TransientInferenceError(f"props_http_{res.status}") from exc

    def count_prompt_tokens(self, messages: list[dict[str, Any]]) -> int:
        tpl = self.request("POST", "/apply-template", {"messages": messages}, timeout_s=30.0)
        if tpl.status != 200 or not isinstance(tpl.body, dict) or not isinstance(tpl.body.get("prompt"), str):
            raise TransientInferenceError(f"apply_template_http_{tpl.status}")
        tok = self.request(
            "POST",
            "/tokenize",
            {"content": tpl.body["prompt"], "add_special": True, "parse_special": True},
            timeout_s=30.0,
        )
        if tok.status != 200 or not isinstance(tok.body, dict) or not isinstance(tok.body.get("tokens"), list):
            raise TransientInferenceError(f"tokenize_http_{tok.status}")
        return len(tok.body["tokens"])

    def chat_structured(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        *,
        max_tokens: int,
        model: str | None = None,
        schema_name: str = "salida",
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """One structured completion. Returns {"output", "usage", "timings", "finish_reason"}."""
        body: dict[str, Any] = {
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        }
        if model:
            body["model"] = model
        res = self.request("POST", "/v1/chat/completions", body, timeout_s=timeout_s)
        if res.status == 400 and _error_type(res.body) == "exceed_context_size_error":
            err = res.body.get("error", {}) if isinstance(res.body, dict) else {}
            raise ContextExceededError(n_prompt_tokens=err.get("n_prompt_tokens"), n_ctx=err.get("n_ctx"))
        if res.status in (429, 500, 502, 503, 504):
            raise TransientInferenceError(f"completion_http_{res.status}")
        if res.status != 200 or not isinstance(res.body, dict):
            raise OutputInvalidError(f"completion_http_{res.status}")
        try:
            choice = res.body["choices"][0]
            content = choice["message"]["content"]
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise OutputInvalidError("completion_shape") from exc
        if finish == "length":
            raise OutputInvalidError("output_truncated")
        try:
            output = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise OutputInvalidError("output_not_json") from exc
        if not isinstance(output, dict):
            raise OutputInvalidError("output_not_object")
        return {
            "output": output,
            "usage": res.body.get("usage"),
            "timings": res.body.get("timings"),
            "finish_reason": finish,
        }


def _error_type(body: Any) -> str | None:
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        t = body["error"].get("type")
        return str(t) if t is not None else None
    return None


class HttpProber:
    """Real readiness: health 200 **and** the configured alias listed **and** (while starting) one structured
    canary completion that parses against its schema. Liveness while ready uses `/health` only, which does not
    decode and therefore never counts as inference activity."""

    def __init__(
        self,
        client: LlamaClient,
        *,
        model_alias: str,
        canary: bool = True,
        canary_timeout_s: float = 180.0,
        canary_max_tokens: int = 1024,
    ) -> None:
        self.client = client
        self.model_alias = model_alias
        self.canary = canary
        self.canary_timeout_s = canary_timeout_s
        self.canary_max_tokens = canary_max_tokens

    def probe(self, *, full: bool) -> Readiness:
        try:
            health = self.client.health()
        except TransientInferenceError as exc:
            return Readiness("unreachable", exc.code)
        if health.status == 503:
            return Readiness("loading", "health_503")
        if health.status != 200:
            return Readiness("unhealthy", f"health_{health.status}")
        if not full:
            return Readiness("ready", "health_ok")
        try:
            if self.model_alias not in self.client.model_ids():
                return Readiness("unhealthy", "model_alias_missing")
            if self.canary:
                res = self.client.chat_structured(
                    CANARY_MESSAGES,
                    CANARY_SCHEMA,
                    max_tokens=self.canary_max_tokens,
                    model=self.model_alias,
                    schema_name="canary",
                    timeout_s=self.canary_timeout_s,
                )
                if not isinstance(res["output"].get("ok"), bool):
                    return Readiness("unhealthy", "canary_schema")
                return Readiness("ready", "canary_ok", verified=True)
        except TransientInferenceError as exc:
            return Readiness("loading", f"canary:{exc.code}")
        except InferenceError as exc:
            return Readiness("unhealthy", f"canary:{exc.code}")
        return Readiness("ready", "models_ok", verified=True)
