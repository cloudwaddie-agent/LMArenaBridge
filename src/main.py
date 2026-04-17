from typing import Optional, Dict, List, Any
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit

# Pydantic models for Anthropic API
from pydantic import BaseModel, Field
from typing import Literal

import uuid
import time
import asyncio
import json
import re
import os
import httpx
import base64
import hashlib
from fastapi import FastAPI, Request, HTTPException, Depends, Form
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette import status
from contextlib import asynccontextmanager

# Import from modules
from . import constants
from .config import get_config, save_config, get_models, save_models
from .state import chat_sessions, model_usage_stats, api_key_usage, current_token_index
from .auth import (
    decode_auth_token,
    encode_auth_token,
    is_token_expired,
    get_next_auth_token,
    maybe_refresh_expired_auth_tokens,
    refresh_arena_auth_token_via_supabase,
    refresh_arena_auth_token_via_lmarena_http,
    is_probably_valid_arena_auth_token,
    normalize_user_agent_value,
)
from .browser_utils import (
    _is_windows,
    _maybe_apply_camoufox_window_mode,
    click_turnstile,
    safe_page_evaluate,
    _cancel_background_task,
)
from .recaptcha import get_recaptcha_v3_token, get_cached_recaptcha_token, refresh_recaptcha_token
from .transport import (
    BrowserFetchStreamResponse,
    UserscriptProxyStreamResponse,
    fetch_lmarena_stream_via_chrome,
    fetch_lmarena_stream_via_camoufox,
    fetch_via_proxy_queue,
    push_proxy_chunk,
    camoufox_proxy_worker,
)


DEBUG = constants.DEBUG
PORT = constants.PORT
HTTPStatus = constants.HTTPStatus
STATUS_MESSAGES = constants.STATUS_MESSAGES

def debug_print(*args, **kwargs):
    """Print debug messages if DEBUG is enabled."""
    if DEBUG:
        print(*args, **kwargs)


# FastAPI app
def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="LMArena Bridge",
        description="OpenAI-compatible API bridge to LMArena",
        version="1.0.0",
    )
    
    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    return app


app = create_app()


# Anthropic API Models
class AnthropicContentBlock(BaseModel):
    type: Literal["text", "image", "tool_use", "tool_result"]
    text: Optional[str] = None
    source: Optional[Dict[str, Any]] = None


class AnthropicMessageParam(BaseModel):
    role: Literal["user", "assistant"]
    content: Union[str, List[AnthropicContentBlock]]


class AnthropicMessageRequest(BaseModel):
    model: str
    messages: List[AnthropicMessageParam]
    max_tokens: int
    system: Optional[Union[str, List[AnthropicContentBlock]]] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    stop_sequences: Optional[List[str]] = None
    stream: Optional[bool] = False


class AnthropicUsage(BaseModel):
    input_tokens: int
    output_tokens: int


class AnthropicContentResponse(BaseModel):
    type: Literal["text"]
    text: str


class AnthropicMessageResponse(BaseModel):
    id: str
    type: Literal["message"]
    role: Literal["assistant"]
    content: List[AnthropicContentResponse]
    model: str
    stop_reason: Optional[str] = None
    usage: AnthropicUsage


def convert_anthropic_messages_to_openai(messages: List[AnthropicMessageParam]) -> List[Dict[str, Any]]:
    """Convert Anthropic messages to OpenAI format."""
    openai_messages = []
    for msg in messages:
        if isinstance(msg.content, str):
            openai_messages.append({"role": msg.role, "content": msg.content})
        else:
            content_parts = []
            for block in msg.content:
                if block.type == "text" and block.text:
                    content_parts.append({"type": "text", "text": block.text})
                elif block.type == "image" and block.source:
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{block.source.get('media_type', 'image/png')};base64,{block.source.get('data', '')}"}
                    })
            openai_messages.append({"role": msg.role, "content": content_parts})
    return openai_messages


def convert_openai_response_to_anthropic(openai_response: Dict[str, Any], model: str) -> AnthropicMessageResponse:
    """Convert OpenAI response to Anthropic format."""
    if "error" in openai_response:
        raise HTTPException(status_code=400, detail=openai_response["error"].get("message", "Unknown error"))

    choices = openai_response.get("choices", [])
    if not choices:
        raise HTTPException(status_code=500, detail="Empty response from model")

    message = choices[0].get("message", {})
    content = message.get("content", "")
    reasoning_content = message.get("reasoning_content", "")

    usage = openai_response.get("usage", {})
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)

    content_blocks = []
    if reasoning_content:
        content_blocks.append(AnthropicContentResponse(type="text", text=f"<thinking>{reasoning_content}</thinking>\n\n"))
    if content:
        content_blocks.append(AnthropicContentResponse(type="text", text=content))

    return AnthropicMessageResponse(
        id=openai_response.get("id", f"msg_{uuid.uuid4()}"),
        type="message",
        role="assistant",
        content=content_blocks,
        model=model,
        stop_reason="end_turn",
        usage=AnthropicUsage(input_tokens=input_tokens, output_tokens=output_tokens)
    )


@app.post("/v1/messages")
async def anthropic_messages(request: AnthropicMessageRequest, raw_request: Request, api_key: dict = Depends(rate_limit_api_key)):
    """
    Anthropic Messages API endpoint.
    Translates Anthropic-style requests to OpenAI-style and processes through LMArena.
    """
    debug_print("\n" + "="*80)
    debug_print("🟣 ANTHROPIC MESSAGES REQUEST RECEIVED")
    debug_print("="*80)
    debug_print(f"🤖 Model: {request.model}")
    debug_print(f"💬 Messages: {len(request.messages)}")
    debug_print(f"🌊 Stream: {request.stream}")

    # Convert Anthropic messages to OpenAI format
    openai_messages = convert_anthropic_messages_to_openai(request.messages)

    # Add system message if present
    if request.system:
        openai_messages.insert(0, {"role": "system", "content": request.system})

    # Build OpenAI-style request body
    openai_body = {
        "model": request.model,
        "messages": openai_messages,
        "max_tokens": request.max_tokens,
        "stream": request.stream,
    }
    if request.temperature is not None:
        openai_body["temperature"] = request.temperature

    debug_print(f"📦 Converted to OpenAI format")

    # Create a mock request object for the chat completions handler
    class MockRequest:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

        async def is_disconnected(self):
            return await raw_request.is_disconnected()

    mock_request = MockRequest(openai_body)

    if request.stream:
        # Streaming response
        async def anthropic_stream_generator():
            """Generate Anthropic-formatted SSE stream from OpenAI stream."""
            message_id = f"msg_{uuid.uuid4()}"
            input_tokens = 0
            output_tokens = 0
            accumulated_text = ""
            accumulated_reasoning = ""

            # Send message_start event first
            message_start = {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": request.model,
                    "stop_reason": None,
                    "usage": {"input_tokens": sum(len(str(m.get("content", ""))) for m in openai_messages), "output_tokens": 0}
                }
            }
            yield f"event: message_start\ndata: {json.dumps(message_start)}\n\n"

            # Send content_block_start event
            content_block_start = {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""}
            }
            yield f"event: content_block_start\ndata: {json.dumps(content_block_start)}\n\n"

            try:
                # Call api_chat_completions and await the result first
                result = await api_chat_completions(mock_request, api_key)

                if not isinstance(result, StreamingResponse):
                    # Handle non-streaming result or error
                    if isinstance(result, dict) and "error" in result:
                        yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': result['error']})}\n\n"
                    else:
                        # Handle unexpected non-streaming success as an error or convert if needed
                        yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'message': 'Unexpected non-streaming response'}})}\n\n"
                    return

                # Handle the streaming response
                buffer = ""
                async for chunk in result.body_iterator:
                    chunk_str = chunk.decode('utf-8') if isinstance(chunk, bytes) else str(chunk)
                    buffer += chunk_str
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        if not line.startswith('data: '):
                            continue
                        data = line[6:]
                        if data == '[DONE]':
                            break

                        try:
                            chunk_data = json.loads(data)
                            if "error" in chunk_data:
                                error_event = {
                                    "type": "error",
                                    "error": chunk_data["error"]
                                }
                                yield f"event: error\ndata: {json.dumps(error_event)}\n\n"
                                return

                            delta = chunk_data.get("choices", [{}])[0].get("delta", {})
                            if "content" in delta:
                                content = delta["content"]
                                accumulated_text += content
                                content_block = {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {"type": "text_delta", "text": content}
                                }
                                yield f"event: content_block_delta\ndata: {json.dumps(content_block)}\n\n"

                            if "reasoning_content" in delta:
                                reasoning = delta["reasoning_content"]
                                accumulated_reasoning += reasoning

                        except json.JSONDecodeError:
                            continue

            except Exception as e:
                error_event = {
                    "type": "error",
                    "error": {"message": str(e), "type": "internal_error"}
                }
                yield f"event: error\ndata: {json.dumps(error_event)}\n\n"
                return

            # Send content_block_stop event
            content_block_stop = {
                "type": "content_block_stop",
                "index": 0
            }
            yield f"event: content_block_stop\ndata: {json.dumps(content_block_stop)}\n\n"

            # Send message_delta event with usage
            message_delta = {
                "type": "message_delta",
                "delta": {
                    "stop_reason": "end_turn"
                },
                "usage": {
                    "output_tokens": len(accumulated_text)
                }
            }
            yield f"event: message_delta\ndata: {json.dumps(message_delta)}\n\n"

            # Send message_stop event (simplified format)
            message_stop = {"type": "message_stop"}
            yield f"event: message_stop\ndata: {json.dumps(message_stop)}\n\n"

        return StreamingResponse(
            anthropic_stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
    else:
        # Non-streaming response
        result = await api_chat_completions(mock_request, api_key)

        if isinstance(result, StreamingResponse):
            # If we got a streaming response but weren't expecting one,
            # read it and convert
            full_response_text = ""
            async for chunk in result.body_iterator:
                chunk_str = chunk.decode('utf-8') if isinstance(chunk, bytes) else str(chunk)
                for line in chunk_str.strip().split('\n'):
                    if line.startswith('data: '):
                        data = line[6:]
                        if data == '[DONE]':
                            break
                        try:
                            chunk_data = json.loads(data)
                            delta = chunk_data.get("choices", [{}])[0].get("delta", {})
                            if "content" in delta:
                                full_response_text += delta["content"]
                        except json.JSONDecodeError:
                            continue

            return {
                "id": f"msg_{uuid.uuid4()}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": full_response_text}],
                "model": request.model,
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 0, "output_tokens": 0}
            }

        if isinstance(result, dict) and "error" in result:
            raise HTTPException(status_code=400, detail=result["error"].get("message", "Unknown error"))

        return convert_openai_response_to_anthropic(result, request.model)


if __name__ == "__main__":
    # Avoid crashes on Windows consoles with non-UTF8 code pages (e.g., GBK) when printing emojis.
    try:
        import sys

        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 60)
    print("🚀 LMArena Bridge Server Starting...")
    print("=" * 60)
    print(f"📍 Dashboard: http://localhost:{PORT}/dashboard")
    print(f"🔐 Login: http://localhost:{PORT}/login")
    print(f"📚 API Base URL: http://localhost:{PORT}/api/v1")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
