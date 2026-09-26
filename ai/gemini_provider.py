"""
Google Gemini provider — uses the REST SSE streaming API directly (no extra deps).
Default model: gemini-2.5-flash (fast, cheap, vision-capable).
"""

import json
import logging
from typing import AsyncIterator, List

import httpx

from ai.base_provider import BaseLLMProvider, Message
from config import cfg

_log = logging.getLogger("lashy.gemini")

DEFAULT_MODEL = "gemini-3-flash-preview"
FALLBACK_MODELS = ["gemini-3-flash-preview", "gemini-3.6-flash", "gemini-3.8-flash"]
STREAM_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent"
)


class GeminiProvider(BaseLLMProvider):

    def __init__(self):
        self._api_key = cfg.google_api_key
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10., read=35., write=10., pool=10.))

    async def stream_response(
        self,
        user_text: str,
        screenshots_b64: List[str],
        history: List[Message],
        system_prompt: str,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        chosen_model = model or DEFAULT_MODEL

        contents = []
        for msg in history:
            role = "user" if msg.role == "user" else "model"
            contents.append({
                "role": role,
                "parts": [{"text": msg.content}],
            })

        parts: list = []
        for img_b64 in screenshots_b64:
            parts.append({
                "inline_data": {"mime_type": "image/jpeg", "data": img_b64},
            })
        parts.append({"text": user_text})
        contents.append({"role": "user", "parts": parts})

        body = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.7},
        }

        models_to_try = [chosen_model]
        if model is None:
            for fb in FALLBACK_MODELS:
                if fb not in models_to_try:
                    models_to_try.append(fb)

        last_exc = None
        for m in models_to_try:
            url = f"{STREAM_URL.format(model=m)}?alt=sse&key={self._api_key}"
            yielded_any = False
            try:
                async with self._client.stream("POST", url, json=body) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        for cand in obj.get("candidates", []):
                            for part in cand.get("content", {}).get("parts", []):
                                text = part.get("text", "")
                                if text:
                                    yielded_any = True
                                    yield text
                return
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                # httpx exceptions contain the authenticated request URL. Never
                # expose it in logs, desktop notifications, or the chat panel.
                _log.warning("Gemini model %s failed (yielded=%s): %s, HTTP %s",
                             m, yielded_any, type(e).__name__, status)
                last_exc = e
                if not yielded_any and m != models_to_try[-1]:
                    _log.info("Retrying with fallback model %s...", models_to_try[-1])
                    continue
                if status and status >= 500:
                    message = f"Gemini is temporarily unavailable (HTTP {status}). Please try again."
                elif status == 429:
                    message = "Gemini's usage limit was reached. Please wait or choose another model."
                elif status in (401, 403):
                    message = "Gemini could not authorize this request. Check the API configuration."
                elif isinstance(e, httpx.TimeoutException):
                    message = "Gemini took too long to respond. Please try again."
                else:
                    message = "Gemini could not complete this request. Please try again or choose another model."
                raise RuntimeError(message) from None

    async def close(self):
        await self._client.aclose()

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={self._api_key}"
                )
                return r.status_code == 200
        except Exception:
            return False
