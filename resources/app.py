
import os
import json
import logging

import openlit
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry import trace
from openai import OpenAI  # ← for token/cost tracing

# ─── 0) init OpenLit ────────────────────────────────────────────────────
openlit.init(
    otlp_endpoint="http://datakit:9529/otel",
    application_name="ollama-proxy",
    environment="demo",
)

# set up Python logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("ollama-proxy")

# ─── 1) real-ollama base ─────────────────────────────────────────────────
base = os.getenv("REAL_OLLAMA_URL", "http://real-ollama:11434").rstrip("/")
if base.endswith("/v1"):
    base = base[: -len("/v1")]

# ─── 2) tracer ───────────────────────────────────────────────────────────
tracer = trace.get_tracer("ollama-proxy")

# ─── 3) OpenAI SDK client for chat/completions ──────────────────────────
oa = OpenAI(api_key="ollama", base_url=f"{base}/v1")

# ─── 4) HTTPX client for everything else ────────────────────────────────
client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=600.0))

# ─── 5) FastAPI + instrument FastAPI & HTTPX ───────────────────────────
app = FastAPI()
# guard against double‑instrumentation
try:
    FastAPIInstrumentor().instrument_app(app)
except Exception:
    logger.debug("FastAPI already instrumented", exc_info=True)
try:
    HTTPXClientInstrumentor().instrument()
except Exception:
    logger.debug("HTTPX already instrumented", exc_info=True)

# ─────────────────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────────────────
def _safe_json_loads(text: str):
    """Try to parse JSON, return {} on failure."""
    try:
        return json.loads(text)
    except Exception:
        return {}

def _extract_last_json_from_blob(blob: str, prefix: str = "stringValue:"):
    """
    Return the *last* valid JSON object inside a possibly‑streamed blob.
    """
    if not blob:
        return {}
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()] or [blob]
    for line in reversed(lines):
        candidate = line.split(prefix, 1)[-1] if (prefix and prefix in line) else line
        data = _safe_json_loads(candidate.strip())
        if data:
            return data
    return {}

def _aggregate_completion_from_stream(blob: str, prefix: str = "") -> str:
    """Concatenate assistant message fragments from a streamed response."""
    if not blob:
        return ""
    pieces = []
    for ln in (ln.strip() for ln in blob.splitlines() if ln.strip()):
        candidate = ln.split(prefix, 1)[-1] if (prefix and prefix in ln) else ln
        obj = _safe_json_loads(candidate)
        if not obj:
            continue
        # Ollama style: {"message": {"role": "assistant", "content": "..."}}
        msg = obj.get("message", {})
        if msg.get("role") == "assistant":
            pieces.append(msg.get("content", ""))
        else:
            # OpenAI stream style
            content = obj.get("choices", [{}])[0].get("delta", {}).get("content")
            if content:
                pieces.append(content)
    return "".join(pieces)

def _extract_first_completion_text(data: dict):
    if not data:
        return ""
    try:
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception:
        return data.get("choices", [{}])[0].get("text", "")

def _extract_usage_from_data(data: dict):
    if not data:
        return None, None, None
    usage = data.get("usage", {})
    if usage:
        return usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens")
    if "prompt_eval_count" in data or "eval_count" in data:
        prompt = data.get("prompt_eval_count")
        completion = data.get("eval_count")
        total = prompt + completion if (prompt is not None and completion is not None) else None
        return prompt, completion, total
    return None, None, None

# ─── 6) Special-case chat/completions → OpenAI SDK ──────────────────────
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    dify_options = payload.pop("options", None) or {}
    payload.pop("format", None)

    messages = payload.get("messages", [])
    user_prompt = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    system_messages = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")

    request_model = payload.get("model", "")
    request_temperature = payload.get("temperature") or dify_options.get("temperature")

    with tracer.start_as_current_span("chat.completions") as span:
        span.set_attribute("gen_ai.prompt", user_prompt)
        span.set_attribute("gen_ai.system", system_messages)
        span.set_attribute("gen_ai.request.model", request_model)
        if request_temperature is not None:
            span.set_attribute("gen_ai.request.temperature", request_temperature)

        span.set_attribute("proxy.request.body", json.dumps(payload, default=str))
        logger.debug("♻️ chat/completions payload → %s", json.dumps(payload, default=str))

        resp = oa.chat.completions.create(**payload)
        data = resp.model_dump()

        completion_text = _extract_first_completion_text(data)
        in_tokens, out_tokens, total_tokens = _extract_usage_from_data(data)

        span.set_attribute("gen_ai.completion", completion_text)
        if in_tokens is not None:
            span.set_attribute("gen_ai.usage.input_tokens", in_tokens)
        if out_tokens is not None:
            span.set_attribute("gen_ai.usage.output_tokens", out_tokens)
        if total_tokens is not None:
            span.set_attribute("gen_ai.usage.total_tokens", total_tokens)

        span.set_attribute("proxy.response.body", json.dumps(data, default=str))
        logger.debug("✅ chat/completions response → %s", json.dumps(data, default=str))

    return JSONResponse(content=data)

# ─── 7) catch-all pure proxy for everything else ────────────────────────
@app.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
)
async def dumb_proxy(path: str, request: Request):
    target_url = f"{base}/{path.lstrip('/')}"
    body_bytes = await request.body()
    body_str = body_bytes.decode(errors="replace")
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

    # parse request JSON
    req_data = _extract_last_json_from_blob(body_str)

    req_model = req_data.get("model", "")
    req_temperature = req_data.get("temperature") or req_data.get("options", {}).get("temperature")
    messages = req_data.get("messages", [])
    req_user_prompt = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    req_system_prompt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")

    with tracer.start_as_current_span("httpx.proxy_request") as span:
        span.set_attribute("http.target_url", target_url)
        span.set_attribute("proxy.request.body", body_str)
        logger.debug("➡️  Proxy → %s %s", request.method, target_url)
        logger.debug("    request body: %s", body_str)

        span.set_attribute("gen_ai.prompt", req_user_prompt)
        span.set_attribute("gen_ai.system", req_system_prompt)
        if req_model:
            span.set_attribute("gen_ai.request.model", req_model)
        if req_temperature is not None:
            span.set_attribute("gen_ai.request.temperature", req_temperature)

        resp = await client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            params=request.query_params,
            content=body_bytes,
        )

        resp_text = resp.content.decode(errors="replace")
        span.set_attribute("proxy.response.body", resp_text)
        logger.debug("⬅️  Upstream %s body: %s", resp.status_code, resp_text)

        # ── NEW: aggregate streamed completion ───────────────────────────
        completion_text = _aggregate_completion_from_stream(resp_text, prefix="")
        if not completion_text:  # fallback – rarely needed
            resp_data = _extract_last_json_from_blob(resp_text, prefix="")
            completion_text = _extract_first_completion_text(resp_data) or resp_data.get("message", {}).get("content", "")

        resp_data = _extract_last_json_from_blob(resp_text, prefix="")
        in_tokens, out_tokens, total_tokens = _extract_usage_from_data(resp_data)

        span.set_attribute("gen_ai.completion", completion_text)
        if in_tokens is not None:
            span.set_attribute("gen_ai.usage.input_tokens", in_tokens)
        if out_tokens is not None:
            span.set_attribute("gen_ai.usage.output_tokens", out_tokens)
        if total_tokens is not None:
            span.set_attribute("gen_ai.usage.total_tokens", total_tokens)

    safe_headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("content-encoding", "transfer-encoding")}

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=safe_headers,
        media_type=resp.headers.get("content-type"),
    )
