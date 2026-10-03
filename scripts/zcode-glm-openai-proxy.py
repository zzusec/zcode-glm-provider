#!/usr/bin/env python3
"""ZCode GLM OpenAI-compatible HTTP provider.

把本机 ZCode 的 GLM（bigmodel start-plan，经 zcode CLI 无头模式）反代成
OpenAI /v1/chat/completions + /v1/models 接口。

实现要点：
- 每个请求 spawn 一次 `zcode -p <prompt> --json`，解析 response/usage；
  CLI 输出是结束后一次性给出，因此流式请求在完成后分块伪流式下发。
- 工具全部禁用（--disallowed-tools 广列表），只做纯对话，避免 API 调用方
  通过提示注入触发本地工具执行。
- 请求用信号量限并发（默认 2），单请求超时 300s。
- 供应商解析依赖 ~/.zcode/v2/provider_config.json 的首条 rule
  （bigmodel-start-plan，含签名凭据与 JWT 认证头），详见该文件。

环境变量：
  ZCODE_GLM_PROXY_PORT   监听端口（默认 15730）
  ZCODE_GLM_PROXY_TOKEN  可选 Bearer Token；未设置时不做鉴权（仅监听 127.0.0.1）
"""

import asyncio
import json
import os
import time
import uuid

from aiohttp import web

ZCODE_BIN = "/opt/homebrew/bin/zcode"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("ZCODE_GLM_PROXY_PORT", "15730"))
AUTH_TOKEN = os.environ.get("ZCODE_GLM_PROXY_TOKEN", "").strip()
REQUEST_TIMEOUT = 300
MAX_CONCURRENCY = 2
CWD = "/tmp"

MODEL_ID = "GLM-5.3-Flash"
MODELS = [MODEL_ID]

DISALLOWED_TOOLS = (
    "Bash Read Edit Write MultiEdit NotebookEdit Glob Grep WebFetch WebSearch "
    "Agent Task TodoWrite TodoRead AskUserQuestion EnterPlanMode ExitPlanMode "
    "Skill Compat SendMessage ReadSessionContext OffPeakCreate OffPeakList "
    "CreateWorkflow AmendWorkflow EvalWorkflowSnippet SaveWorkflow "
    "ListSavedWorkflows GetWorkflowRun ListWorkflowRuns ResumeWorkflowRun "
    "ResolveWorkflowQuestion CronCreate CronDelete CronList CronUpdate"
).split()

_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)


def _text_of(content):
    """OpenAI content（str 或 parts 列表）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text", "")))
        return "\n".join(parts)
    return "" if content is None else str(content)


def build_prompt(system_parts, history):
    """(system 文本列表, [(role, text)]) → 单个 prompt。"""
    if not history:
        raise ValueError("messages 中没有可用的 user/assistant 消息")
    last_role, last_text = history[-1]
    prior = history[:-1]
    lines = []
    if system_parts:
        lines.append("[System]\n" + "\n".join(system_parts))
    if prior:
        lines.append("[Conversation history]")
        for role, text in prior:
            lines.append(("User: " if role == "user" else "Assistant: ") + text)
    if last_role == "assistant":
        lines.append("[Conversation history]")
        lines.append("Assistant: " + last_text)
        lines.append("[Task]\nPlease continue appropriately.")
    else:
        if prior or system_parts:
            lines.append("[New user message]")
        lines.append(last_text)
    return "\n\n".join(lines).strip()[:200_000]


def flatten_messages(messages):
    """OpenAI 消息列表 → 单个 prompt（system + 历史 + 最后一条用户消息）。"""
    system_parts, history = [], []
    for m in messages or []:
        role = m.get("role")
        text = _text_of(m.get("content"))
        if not text:
            continue
        if role in ("system", "developer"):
            system_parts.append(text)
        elif role in ("user", "assistant"):
            history.append((role, text))
    return build_prompt(system_parts, history)


def anthropic_pairs(body):
    """Anthropic /v1/messages 请求体 → (system 文本列表, [(role, text)])。"""
    system = body.get("system")
    if isinstance(system, str):
        parts = [system] if system.strip() else []
    elif isinstance(system, list):
        parts = [str(b.get("text", "")) for b in system
                 if isinstance(b, dict) and b.get("type") == "text"]
        parts = [p for p in parts if p.strip()]
    else:
        parts = []
    history = []
    for m in body.get("messages") or []:
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = m.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(str(b.get("text", "")) for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = ""
        if text.strip():
            history.append((role, text))
    return parts, history


async def run_zcode(prompt):
    """跑一次 zcode -p，返回 (response_text, usage, session_id)。"""
    cmd = [
        ZCODE_BIN, "-p", prompt, "--json", "--no-color",
        "--cwd", CWD,
        "--disallowed-tools", *DISALLOWED_TOOLS,
    ]
    env = dict(os.environ)
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    async with _semaphore:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CWD,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"zcode 执行超时（{REQUEST_TIMEOUT}s）")
    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        detail = next((l for l in reversed(err) if l.strip()), "unknown error")
        raise RuntimeError(f"zcode 失败(rc={proc.returncode}): {detail[:500]}")
    try:
        data = json.loads(stdout.decode("utf-8", "replace"))
    except Exception:
        text = stdout.decode("utf-8", "replace").strip()
        if not text:
            raise RuntimeError("zcode 输出为空")
        return text, {}, None
    return (
        data.get("response") or "",
        data.get("usage") or {},
        data.get("sessionId"),
    )


def check_auth(request):
    if not AUTH_TOKEN:
        return None
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {AUTH_TOKEN}":
        return None
    return web.json_response(
        {"error": {"message": "Invalid API key", "type": "auth_error"}}, status=401
    )


def chunk_response_id():
    return "chatcmpl-" + uuid.uuid4().hex[:24]


def sse_event(payload):
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def handle_models(request):
    resp = check_auth(request)
    if resp:
        return resp
    return web.json_response({
        "object": "list",
        "data": [{
            "id": MODEL_ID,
            "object": "model",
            "owned_by": "zcode-local",
        }],
    })


async def handle_chat(request):
    resp = check_auth(request)
    if resp:
        return resp
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
            status=400,
        )
    messages = body.get("messages") or []
    stream = bool(body.get("stream"))
    try:
        prompt = flatten_messages(messages)
    except ValueError as e:
        return web.json_response(
            {"error": {"message": str(e), "type": "invalid_request_error"}}, status=400
        )

    started = time.time()
    try:
        text, usage, session_id = await run_zcode(prompt)
    except RuntimeError as e:
        return web.json_response(
            {"error": {"message": str(e), "type": "upstream_error"}}, status=502
        )
    elapsed = time.time() - started

    created = int(time.time())
    prompt_tokens = int(usage.get("inputTokens") or 0)
    completion_tokens = int(usage.get("outputTokens") or 0)

    if not stream:
        return web.json_response({
            "id": chunk_response_id(),
            "object": "chat.completion",
            "created": created,
            "model": MODEL_ID,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        })

    # CLI 无真流式：完成后分块下发（伪流式）
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)
    cid = chunk_response_id()
    first = sse_event({
        "id": cid, "object": "chat.completion.chunk", "created": created,
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
    })
    await response.write(first.encode())
    STEP = 80
    for i in range(0, len(text), STEP):
        piece = text[i:i + STEP]
        ev = sse_event({
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
        })
        await response.write(ev.encode())
    await response.write(sse_event({
        "id": cid, "object": "chat.completion.chunk", "created": created,
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }).encode())
    await response.write(b"data: [DONE]\n\n")
    await response.write_eof()
    print(f"[zcode-glm-proxy] {created} session={session_id} {elapsed:.1f}s "
          f"tokens={prompt_tokens}+{completion_tokens}", flush=True)
    return response


async def handle_messages(request):
    """Anthropic /v1/messages 兼容端点（供 Claude Code 等使用）。"""
    resp = check_auth(request)
    if resp:
        return resp
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"type": "error", "error": {"type": "invalid_request_error", "message": "Invalid JSON body"}},
            status=400,
        )
    system_parts, history = anthropic_pairs(body)
    try:
        prompt = build_prompt(system_parts, history)
    except ValueError as e:
        return web.json_response(
            {"type": "error", "error": {"type": "invalid_request_error", "message": str(e)}},
            status=400,
        )
    requested_model = body.get("model") or MODEL_ID
    try:
        text, usage, session_id = await run_zcode(prompt)
    except RuntimeError as e:
        return web.json_response(
            {"type": "error", "error": {"type": "api_error", "message": str(e)}},
            status=502,
        )
    in_tok = int(usage.get("inputTokens") or 0)
    out_tok = int(usage.get("outputTokens") or 0)
    msg_id = "msg_" + uuid.uuid4().hex[:24]

    if not body.get("stream"):
        return web.json_response({
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": requested_model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        })

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)

    def sse(etype, data):
        return f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    await response.write(sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "model": requested_model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": in_tok, "output_tokens": 0},
        },
    }).encode())
    await response.write(sse("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    }).encode())
    STEP = 80
    for i in range(0, len(text), STEP):
        await response.write(sse("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": text[i:i + STEP]},
        }).encode())
    await response.write(sse("content_block_stop", {
        "type": "content_block_stop", "index": 0,
    }).encode())
    await response.write(sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": out_tok},
    }).encode())
    await response.write(sse("message_stop", {"type": "message_stop"}).encode())
    await response.write_eof()
    print(f"[zcode-glm-proxy] anthropic session={session_id} tokens={in_tok}+{out_tok}", flush=True)
    return response


def responses_pairs(body):
    """OpenAI /v1/responses 请求体 → (system 文本列表, [(role, text)])。"""
    system_parts = []
    instr = body.get("instructions")
    if isinstance(instr, str) and instr.strip():
        system_parts.append(instr)
    history = []
    inp = body.get("input")
    items = [{"role": "user", "content": inp}] if isinstance(inp, str) else (inp or [])
    for it in items:
        if not isinstance(it, dict) or it.get("type") not in (None, "message"):
            continue  # function_call / reasoning / item_reference 等一律跳过
        role = it.get("role") or "user"
        content = it.get("content")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") in (
                        "input_text", "output_text", "text", "refusal") and c.get("text"):
                    texts.append(str(c["text"]))
        text = "\n".join(t for t in texts if t.strip())
        if not text.strip():
            continue
        if role in ("system", "developer"):
            system_parts.append(text)
        else:
            history.append(("assistant" if role == "assistant" else "user", text))
    return system_parts, history


async def handle_responses(request):
    """OpenAI Responses API 兼容端点（codex 0.160 仅支持 wire_api=responses）。"""
    resp = check_auth(request)
    if resp:
        return resp
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
            status=400,
        )
    system_parts, history = responses_pairs(body)
    try:
        prompt = build_prompt(system_parts, history)
    except ValueError as e:
        return web.json_response(
            {"error": {"message": str(e), "type": "invalid_request_error"}}, status=400,
        )
    try:
        text, usage, session_id = await run_zcode(prompt)
    except RuntimeError as e:
        return web.json_response(
            {"error": {"message": str(e), "type": "upstream_error"}}, status=502,
        )
    in_tok = int(usage.get("inputTokens") or 0)
    out_tok = int(usage.get("outputTokens") or 0)
    total = in_tok + out_tok
    created = int(time.time())
    resp_id = "resp_" + uuid.uuid4().hex[:24]
    msg_id = "msg_" + uuid.uuid4().hex[:24]
    message_item = {
        "type": "message", "id": msg_id, "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    usage_obj = {"input_tokens": in_tok, "output_tokens": out_tok, "total_tokens": total}

    def response_obj(status="completed"):
        return {
            "id": resp_id, "object": "response", "created_at": created,
            "status": status, "model": MODEL_ID,
            "output": [dict(message_item)],
            "usage": dict(usage_obj),
            "error": None, "incomplete_details": None,
        }

    if not body.get("stream"):
        return web.json_response(response_obj())

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)

    def ev(etype, data):
        return f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    await response.write(ev("response.created", {
        "type": "response.created", "response": response_obj("in_progress"),
    }).encode())
    await response.write(ev("response.output_item.added", {
        "type": "response.output_item.added", "output_index": 0,
        "item": {"type": "message", "id": msg_id, "role": "assistant",
                 "status": "in_progress", "content": []},
    }).encode())
    STEP = 80
    for i in range(0, len(text), STEP):
        await response.write(ev("response.output_text.delta", {
            "type": "response.output_text.delta", "item_id": msg_id,
            "output_index": 0, "content_index": 0,
            "delta": text[i:i + STEP],
        }).encode())
    await response.write(ev("response.output_text.done", {
        "type": "response.output_text.done", "item_id": msg_id,
        "output_index": 0, "content_index": 0, "text": text,
    }).encode())
    await response.write(ev("response.output_item.done", {
        "type": "response.output_item.done", "output_index": 0,
        "item": message_item,
    }).encode())
    await response.write(ev("response.completed", {
        "type": "response.completed", "response": response_obj(),
    }).encode())
    await response.write_eof()
    print(f"[zcode-glm-proxy] responses session={session_id} tokens={in_tok}+{out_tok}", flush=True)
    return response


async def handle_health(request):
    return web.json_response({"ok": True, "model": MODEL_ID})


app = web.Application()
app.router.add_get("/v1/models", handle_models)
app.router.add_post("/v1/chat/completions", handle_chat)
app.router.add_post("/v1/messages", handle_messages)
app.router.add_post("/v1/responses", handle_responses)
app.router.add_get("/health", handle_health)

if __name__ == "__main__":
    port = int(os.environ.get("ZCODE_GLM_PROXY_PORT", str(LISTEN_PORT)))
    web.run_app(app, host=LISTEN_HOST, port=port, print=None)
