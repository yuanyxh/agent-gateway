"""Request body rewriting, tool-output filtering, and upstream forwarding."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import sys
import time

import aiohttp
from agent_gateway.llm import client as llm_client
from aiohttp import web
from agent_gateway.core.config import ui_language
from agent_gateway.core.db import conn as dbconn
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.proxy.proxy_client import (
    load_client_accel,
    matches_accel_client,
)
from agent_gateway.services.proxy.proxy_output_filter import (
    OUTPUT_FILTER_ENV_KEY,
    filter_tool_outputs,
)
from agent_gateway.services.proxy.proxy_protocol import (
    AnthropicToOpenAIStream,
    OpenAIToAnthropicStream,
    ProtocolConverter,
    _detect_api_format,
    _extract_output_from_sse,
    _extract_tool_name_from_sse,
    _extract_usage_from_sse,
    _inject_classifier_disabled,
    _inject_system_prompt,
    _is_classifier_candidate,
)
from agent_gateway.services.proxy.proxy_provider import (
    api_format_of,
    api_key_of,
    missing_keys,
)
from agent_gateway.services.proxy.proxy_response_note import (
    RESPONSE_NOTE_ENV_KEY,
    NoteBodyBuffer,
    SSENoteInjector,
    strip_notes_from_messages,
)
from agent_gateway.services.proxy.proxy_skill_dedup import dedup_tool_results
from agent_gateway.services.proxy.proxy_usage import (
    DB_PATH,
    TOOL_OUTPUT_DB_PATH,
    extract_tool_outputs,
    init_db,
    init_tool_output_db,
    record_gateway_log,
    record_usage,
)


from typing import TYPE_CHECKING

from agent_gateway.services.proxy.proxy_config import (
    DEFAULT_ANTHROPIC_VERSION,
    OUTPUT_FILTER_JSON,
    RESPONSE_NOTE_ENABLED,
    SKILL_DEDUP_ENABLED,
    UPSTREAM_ACCEPT_ENCODING,
    _BACKGROUND_TASKS,
    _SharedSession,
    _logged_first_body,
    logger,
)

if TYPE_CHECKING:
    from agent_gateway.services.proxy.proxy_state import _ProxyState


def _apply_rewrites(state: _ProxyState) -> None:
    """Client-format rewrites before forwarding; the record copy stays untouched."""
    # skill full-text dedup: from the second copy onward replace with a short note (the
    # criterion is byte-identical content; the proxy cannot see the skill list). Must be
    # after record — the record layer must keep the original, otherwise the measurement
    # yardstick destroys itself.
    if SKILL_DEDUP_ENABLED and not state.raw:
        deduped = dedup_tool_results(
            state.request_body, state.api_format, ui_language()
        )
        if deduped:
            state.note_parts.append("skillDedup")
            state.body_bytes = json.dumps(state.request_body).encode()
            logger.info(
                json.dumps(
                    {
                        "msg": "skill_dedup",
                        "replaced": deduped,
                        "api_format": state.api_format,
                    }
                )
            )

    # Tool-output slimming: over-long head/tail trim + lossless JSON minify, only for
    # shell-like tools (Read etc. are file contents the model explicitly asked for;
    # truncating is corruption). Likewise must be after record.
    filtered = (
        {}
        if state.raw
        else filter_tool_outputs(
            state.request_body,
            state.api_format,
            OUTPUT_FILTER_JSON,
            ui_language(),
        )
    )
    if filtered:
        if filtered.get("saved_bytes"):
            state.note_parts.append("outputFilter")
            state.body_bytes = json.dumps(state.request_body).encode()
        logger.info(
            json.dumps(
                {"msg": "output_filter", "api_format": state.api_format, **filtered}
            )
        )

    # Strip the note lines the model echoed back from earlier turns: the note is injected into
    # the assistant's text block, so it enters the history and the model reproduces that line
    # on its own. Removing it here keeps it out of the upstream context.
    if RESPONSE_NOTE_ENABLED and not state.raw:
        stripped = strip_notes_from_messages(state.request_body.get("messages") or [])
        if stripped:
            state.body_bytes = json.dumps(state.request_body).encode()
            logger.info(
                json.dumps(
                    {
                        "msg": "note_strip",
                        "removed": stripped,
                        "api_format": state.api_format,
                    }
                )
            )

    # Request protocol conversion: client anthropic ↔ provider openai (standard gateway mapping)
    if state.api_format == "anthropic" and state.provider_format == "openai":
        state.request_body = json.loads(state.body_bytes)
        state.request_body = ProtocolConverter.request_anthropic_to_openai(
            state.request_body
        )
        state.body_bytes = json.dumps(state.request_body).encode()
        logger.debug(
            json.dumps(
                {
                    "msg": "request_converted",
                    "direction": "anthropic_to_openai",
                    "provider": state.provider.get("name", ""),
                }
            )
        )
    elif state.api_format == "openai" and state.provider_format == "anthropic":
        state.request_body = json.loads(state.body_bytes)
        state.request_body = ProtocolConverter.request_openai_to_anthropic(
            state.request_body
        )
        state.body_bytes = json.dumps(state.request_body).encode()
        logger.debug(
            json.dumps(
                {
                    "msg": "request_converted",
                    "direction": "openai_to_anthropic",
                    "provider": state.provider.get("name", ""),
                }
            )
        )


async def _forward(state: _ProxyState) -> web.StreamResponse:
    """Send the prepared request upstream, stream the response back, and record usage."""
    # Forward headers (pass through, but replace the auth key by provider)
    #
    # accept-encoding is the **exception**: the client's value is not passed through (Claude
    # Code declares `gzip, deflate, br, zstd`); instead this process declares the whitelist it
    # can decode — see the comment on UPSTREAM_ACCEPT_ENCODING. Skip it and then assign it
    # explicitly, both needed: assigning without skipping leaves both `accept-encoding` (the
    # client's original) and `Accept-Encoding` (the whitelist) keys in the dict, and both
    # headers get sent.
    headers = {}
    skip_headers = {
        "host",
        "content-length",
        "transfer-encoding",
        "accept-encoding",
    }
    for k, v in state.request.headers.items():
        if k.lower() not in skip_headers:
            headers[k] = v
    headers["Accept-Encoding"] = UPSTREAM_ACCEPT_ENCODING
    # OpenAI-protocol clients do not send anthropic-version; the converted request still
    # needs it, otherwise an anthropic provider rejects the call.
    if state.provider_format == "anthropic" and not any(
        k.lower() == "anthropic-version" for k in headers
    ):
        headers["anthropic-version"] = DEFAULT_ANTHROPIC_VERSION
    api_key = api_key_of(state.provider)
    if api_key:
        headers.pop("x-api-key", None)
        headers.pop("authorization", None)
        if state.provider_format == "openai":
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            headers["x-api-key"] = api_key

    start_time = time.monotonic()

    # 503 transient retry: the upstream (deepseek etc. providers) occasionally returns 503
    # ("temporarily unavailable"). When it hits an agent client's safety classification call
    # it causes that client's key operations to be blocked (the failure is client-independent;
    # the early observations came from Claude Code).
    # The policy values come from llm_client (**single authority**): the same class of failure
    # must be handled in both "main forwarding" and "LLM calls", and writing a copy in each
    # place inevitably drifts; a change must take effect globally from one place.
    retry_max = llm_client.DEFAULT_RETRY_503_MAX
    retry_delay = llm_client.DEFAULT_RETRY_503_DELAY
    for attempt in range(retry_max + 1):
        # Diagnostic (temporary): record the stage where the exception occurs, to locate the
        # first scene of a stream interruption. Must be assigned inside try, at the first
        # statement of the loop body, so the outer proxy_error can always read it.
        diag_stage = "connect"
        try:
            # Not merged: combining would reindent the 365-line response block below.
            # The two forms are equivalent: _SharedSession closes nothing on exit.
            async with _SharedSession() as session:  # noqa: SIM117
                async with session.request(
                    method=state.request.method,
                    url=state.upstream,
                    headers=headers,
                    data=state.body_bytes,
                    timeout=aiohttp.ClientTimeout(
                        total=300,
                        sock_connect=10,
                        sock_read=120,
                    ),
                ) as upstream_resp:
                    latency_ms = int((time.monotonic() - start_time) * 1000)

                    # Upstream transient 503: abandon this connection, wait and retry
                    if upstream_resp.status == 503 and attempt < retry_max:
                        logger.warning(
                            json.dumps(
                                {
                                    "msg": "proxy_retry",
                                    "path": state.request.path,
                                    "attempt": attempt + 1,
                                    "latency_ms": latency_ms,
                                    "model": state.request_body.get("model", ""),
                                }
                            )
                        )
                        await asyncio.sleep(retry_delay)
                        continue

                    # Stream the forward response
                    #
                    # content-encoding and content-length must be removed **together**; they
                    # are a pair: the aiohttp client auto-decompresses by default, so the
                    # forwarded body is no longer the bytes the upstream declared. Removing
                    # only content-encoding without content-length is equivalent to lying to
                    # the client with the pre-compression length — either truncating by the
                    # old length or a protocol error.
                    #
                    # Model-name injection additionally lengthens the response body, a second
                    # source of the same problem: if the upstream carries content-length,
                    # copying it truncates by the old length and eats the entire injected tail.
                    #
                    # After removal, aiohttp re-frames it (HTTP/1.1 uses chunked).
                    forward_headers = {
                        k: v
                        for k, v in upstream_resp.headers.items()
                        if k.lower()
                        not in {
                            "transfer-encoding",
                            "content-encoding",
                            "content-length",
                        }
                    }
                    response = web.StreamResponse(
                        status=upstream_resp.status, headers=forward_headers
                    )
                    diag_stage = "prepare"
                    await response.prepare(state.request)
                    diag_stage = "stream"

                    # Collect SSE chunks to extract usage
                    collected_body = bytearray()
                    is_sse = "text/event-stream" in upstream_resp.headers.get(
                        "content-type", ""
                    )
                    # Exception info on stream interruption, for record_usage to record
                    stream_error = ""
                    # Diagnostic (temporary): the time of the last data received from the
                    # upstream. Used to distinguish "the client gave up while the upstream
                    # stalled" from "the connection was directly reset" — the former has a
                    # large since_last_chunk_ms, the latter near 0.
                    last_chunk_at = time.monotonic()

                    # The response must be converted whenever the provider protocol differs
                    # from the client's: openai → anthropic (Claude Code scenario) or the
                    # reverse (an OpenAI-protocol client reaching an anthropic provider).
                    convert_resp = state.provider_format != state.api_format
                    converter = None
                    if convert_resp and is_sse:
                        converter = (
                            OpenAIToAnthropicStream()
                            if state.provider_format == "openai"
                            else AnthropicToOpenAIStream()
                        )

                    try:
                        if converter is not None:
                            # Streaming conversion: buffer openai SSE line by line → anthropic SSE events
                            line_buffer = ""
                            async for chunk in upstream_resp.content.iter_any():
                                last_chunk_at = time.monotonic()
                                line_buffer += chunk.decode("utf-8", errors="replace")
                                lines = line_buffer.split("\n")
                                line_buffer = lines.pop()
                                parts: list[str] = []
                                for line in lines:
                                    line = line.strip()
                                    if not line.startswith("data: "):
                                        continue
                                    data_str = line[6:].strip()
                                    if data_str == "[DONE]":
                                        continue
                                    try:
                                        oc = json.loads(data_str)
                                    except json.JSONDecodeError:
                                        continue
                                    parts.extend(converter.feed(oc))
                                if parts:
                                    raw = "".join(parts).encode()
                                    # collected_body collects the un-injected raw stream:
                                    # the extraction yardstick for usage / output preview
                                    # is not affected by injected content
                                    collected_body.extend(raw)
                                    # The response-side note is not wired into this branch yet (no
                                    # rewrite stats are collected for the converted format).
                                    await response.write(raw)
                            # Wrap up: content_block_stop / message_delta / message_stop
                            if not stream_error:
                                tail = converter.finish()
                                if tail:
                                    raw = "".join(tail).encode()
                                    collected_body.extend(raw)
                                    await response.write(raw)
                        elif convert_resp and not is_sse:
                            # Non-streaming conversion: collect the full JSON body first, then convert as a whole
                            raw_body = bytearray()
                            async for chunk in upstream_resp.content.iter_any():
                                last_chunk_at = time.monotonic()
                                raw_body.extend(chunk)
                            try:
                                resp_json = json.loads(bytes(raw_body))
                                converted = (
                                    ProtocolConverter.response_openai_to_anthropic(
                                        resp_json
                                    )
                                    if state.provider_format == "openai"
                                    else ProtocolConverter.response_anthropic_to_openai(
                                        resp_json
                                    )
                                )
                                payload = json.dumps(converted).encode()
                            except Exception as e:  # noqa: BLE001
                                stream_error = f"convert_error: {e}"
                                payload = bytes(raw_body)
                            await response.write(payload)
                            collected_body.extend(payload)
                        else:
                            # Pass-through (same format). The response-side append is only
                            # enabled when there really is a rewrite to report; otherwise it
                            # stays a pure pass-through — no parsing means zero overhead and
                            # no new failure path.
                            note_sink = None
                            if RESPONSE_NOTE_ENABLED and not state.raw:
                                if is_sse:
                                    note_sink = SSENoteInjector(
                                        state.note_parts, ui_language()
                                    )
                                elif upstream_resp.status < 400:
                                    note_sink = NoteBodyBuffer(
                                        state.note_parts, ui_language()
                                    )
                            async for chunk in upstream_resp.content.iter_any():
                                last_chunk_at = time.monotonic()
                                # Collect SSE chunks to extract usage; error responses such
                                # as 400 are non-SSE JSON, also collected to record the
                                # response body for locating the error (temporary enhancement)
                                if is_sse or upstream_resp.status >= 400:
                                    collected_body.extend(chunk)
                                if note_sink is not None:
                                    out = note_sink.feed(chunk)
                                    if out:
                                        await response.write(out)
                                else:
                                    await response.write(chunk)
                            if note_sink is not None:
                                tail = note_sink.finish()
                                if tail:
                                    await response.write(tail)
                                if getattr(note_sink, "message_starts", 0) > 1:
                                    logger.warning(
                                        json.dumps(
                                            {
                                                "msg": "stream_multiple_message_start",
                                                "count": note_sink.message_starts,
                                                "model": state.request_body.get(
                                                    "model", ""
                                                ),
                                                "path": state.request.path,
                                            }
                                        )
                                    )

                    except (
                        ConnectionResetError,
                        BrokenPipeError,
                        RuntimeError,
                        asyncio.CancelledError,
                    ) as e:
                        # The client has disconnected or the transport is closed; stop forwarding
                        stream_error = f"{type(e).__name__}: {e}"
                        # Diagnostic (temporary): this is the real first scene of the stream
                        # interruption, but previously it was only stored in stream_error and
                        # never logged; if a subsequent write_eof throws again, the outer
                        # proxy_error would record the **second** exception and the first scene
                        # would be lost forever. error_type itself carries direction: Client*
                        # family → writing to the client failed;
                        # ClientPayloadError/ServerDisconnectedError → reading the upstream failed.
                        logger.warning(
                            json.dumps(
                                {
                                    "msg": "stream_interrupted",
                                    "stage": diag_stage,
                                    "error_type": type(e).__name__,
                                    "error": str(e),
                                    "received": len(collected_body),
                                    # absolute time: for comparison with the client's debug logs
                                    "ts": int(time.time()),
                                    # total request duration (compare API_TIMEOUT_MS=300000)
                                    "elapsed_ms": int(
                                        (time.monotonic() - start_time) * 1000
                                    ),
                                    # interval since the last data received from the upstream
                                    "since_last_chunk_ms": int(
                                        (time.monotonic() - last_chunk_at) * 1000
                                    ),
                                }
                            )
                        )

                    # Diagnostic (temporary): write_eof is outside the inner try; when the
                    # connection is already broken it throws **again**
                    # (ClientConnectionResetError), bubbling to the outer proxy_error and
                    # overwriting the first scene; it also skips the subsequent record_usage,
                    # leaving the interruption untraced on the database side too. Swallow it
                    # here and log separately, so the first scene is preserved and
                    # record_usage runs.
                    try:
                        await response.write_eof()
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            json.dumps(
                                {
                                    "msg": "write_eof_failed",
                                    "error_type": type(e).__name__,
                                    "error": str(e),
                                    "stream_error": stream_error,
                                    "received": len(collected_body),
                                }
                            )
                        )
                    diag_stage = "done"

                    # Extract usage / output preview / tool name
                    sse_bytes = bytes(collected_body) if collected_body else b""
                    if converter is not None:
                        # The converter normalizes usage to input_tokens/output_tokens; the
                        # converted stream is in the client's protocol.
                        usage_data = dict(converter.usage)
                        out_format = state.api_format
                    elif convert_resp and not is_sse:
                        converted_usage = {}
                        try:
                            parsed = json.loads(
                                sse_bytes.decode("utf-8", errors="replace")
                            )
                            converted_usage = (
                                parsed.get("usage", {})
                                if isinstance(parsed, dict)
                                else {}
                            )
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass
                        if state.api_format == "openai":
                            usage_data = {
                                "input_tokens": converted_usage.get("prompt_tokens", 0),
                                "output_tokens": converted_usage.get(
                                    "completion_tokens", 0
                                ),
                            }
                        else:
                            usage_data = {
                                "input_tokens": converted_usage.get("input_tokens", 0),
                                "output_tokens": converted_usage.get(
                                    "output_tokens", 0
                                ),
                            }
                        out_format = state.api_format
                    else:
                        usage_data = (
                            _extract_usage_from_sse(
                                sse_bytes, api_format=state.api_format
                            )
                            if is_sse and sse_bytes
                            else None
                        )
                        out_format = state.api_format

                    if usage_data:
                        # Supplement model: some APIs' SSE responses lack a model field
                        if not usage_data.get("model"):
                            usage_data["model"] = state.request_body.get(
                                "model", "unknown"
                            )
                        output_preview = (
                            _extract_output_from_sse(sse_bytes, api_format=out_format)
                            if sse_bytes
                            else ""
                        )
                        tool_name = (
                            _extract_tool_name_from_sse(
                                sse_bytes, api_format=out_format
                            )
                            if sse_bytes
                            else ""
                        )
                        # Extract error info (stream-interruption exception first, then the upstream error body)
                        error_message = stream_error
                        if not error_message and upstream_resp.status >= 400:
                            try:
                                error_body = json.loads(
                                    sse_bytes.decode("utf-8", errors="replace")
                                )
                                if "error" in error_body:
                                    error_data = error_body["error"]
                                    if isinstance(error_data, dict):
                                        error_message = error_data.get(
                                            "message", str(error_data)
                                        )
                                    else:
                                        error_message = str(error_data)
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                pass
                        # Tool outputs (the latest round of tool_result) are persisted
                        # together with usage. The two tables belong to different DB files, and
                        # only here can the api_usage auto-increment id be obtained; writing
                        # them separately would leave api_usage_id always NULL, breaking the
                        # cost-attribution chain. The coverage is exactly the same as api_usage
                        # (neither table records when usage resolution fails or the status is
                        # non-200), so a JOIN has no orphan rows — this is deliberate
                        # alignment, not an omission.
                        tool_outputs = extract_tool_outputs(
                            state.record_request_body, api_format=state.api_format
                        )
                        task = asyncio.create_task(
                            record_usage(
                                state.record_request_body,
                                usage_data,
                                upstream_resp.status,
                                latency_ms,
                                output_preview,
                                error_message,
                                tool_name_override=tool_name,
                                api_format=state.api_format,
                                tool_outputs=tool_outputs,
                            )
                        )
                        _BACKGROUND_TASKS.add(task)
                        task.add_done_callback(_BACKGROUND_TASKS.discard)

                    logger.info(
                        json.dumps(
                            {
                                "msg": "proxy_request",
                                "method": state.request.method,
                                "path": state.request.path,
                                "status": upstream_resp.status,
                                "latency_ms": latency_ms,
                                "model": state.request_body.get("model", ""),
                            }
                        )
                    )

                    # Temporary enhancement: record the >=400 error response body, to locate the specific error of deepseek's context over-limit 400
                    if upstream_resp.status >= 400 and sse_bytes:
                        logger.warning(
                            json.dumps(
                                {
                                    "msg": "proxy_http_error_body",
                                    "model": state.request_body.get("model", ""),
                                    "status": upstream_resp.status,
                                    "latency_ms": latency_ms,
                                    "body": sse_bytes.decode("utf-8", errors="replace")[
                                        :500
                                    ],
                                }
                            )
                        )

                    return response

        except (TimeoutError, aiohttp.ClientError) as e:
            latency_ms = int((time.monotonic() - start_time) * 1000)
            # Connect-stage failures never reached the upstream and wrote nothing to the
            # client, so replaying is safe; from "prepare" on a retry would double the response.
            if (
                attempt < retry_max
                and diag_stage == "connect"
                and isinstance(e, (aiohttp.ClientConnectionError, asyncio.TimeoutError))
            ):
                logger.warning(
                    json.dumps(
                        {
                            "msg": "proxy_retry",
                            "reason": "connection",
                            "path": state.request.path,
                            "attempt": attempt + 1,
                            "error_type": type(e).__name__,
                            "latency_ms": latency_ms,
                        }
                    )
                )
                await asyncio.sleep(retry_delay)
                continue
            logger.error(
                json.dumps(
                    {
                        "msg": "proxy_error",
                        "path": state.request.path,
                        "error": str(e),
                        # Diagnostic (temporary): exception type + stage. Previously only
                        # str(e) was logged, but "Cannot write to closing transport" is thrown
                        # in all three of prepare / writing to the client / write_eof, and the
                        # message alone cannot tell which one it was.
                        "error_type": type(e).__name__,
                        "stage": diag_stage,
                        "latency_ms": latency_ms,
                    }
                )
            )
            return web.json_response(
                {"error": f"upstream_error: {e}"},
                status=502,
            )


#: The value of `created` in the OpenAI-compatible schema (the epoch of 2026-01-01T00:00:00Z).
#: Use a fixed constant rather than the current time: the `created` of the same model list
#: must be stable across responses, otherwise clients caching by that field would
#: continually invalidate.
