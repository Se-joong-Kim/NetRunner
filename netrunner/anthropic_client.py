"""Anthropic Messages API를 호출해 응답 텍스트를 돌려주는 함수를 만든다.

LLMBreaker의 complete 자리에 꽂는다. 표준 라이브러리(urllib)만 쓴다.
모델 이름과 API 키는 호출하는 쪽(진입점)이 정해 넘긴다.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable

from .breaker import CompletionError

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# post(url, headers, body, timeout) -> 응답 본문 bytes. 테스트에서 바꿔 끼운다.
Post = Callable[[str, dict[str, str], bytes, float], bytes]

# 전송 중 끊김, 타임아웃, 불완전한 본문을 모두 이 범위로 잡는다.
_TRANSPORT_ERRORS = (OSError, http.client.HTTPException)


class ModelCallError(CompletionError):
    """모델 호출이 실패했거나 응답에서 텍스트를 찾지 못했다."""


def _post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> bytes:
    """HTTP POST 한 번. 실패는 모두 ModelCallError로 바꾼다.

    오류 본문에는 키가 없지만, 헤더 값은 메시지에 넣지 않는다.
    """
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:200].decode("utf-8", "replace")
        except _TRANSPORT_ERRORS:
            detail = "(본문을 읽지 못함)"
        raise ModelCallError(f"API 오류 {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise ModelCallError(f"API 연결 실패: {e.reason}") from None
    except _TRANSPORT_ERRORS as e:
        raise ModelCallError(f"응답 수신 실패: {type(e).__name__}") from None


def _text_of(payload: bytes) -> str:
    """Messages 응답에서 text 블록을 이어 붙인 문자열을 꺼낸다.

    max_tokens에서 잘린 응답은 뒤가 빠진 것이므로 오류로 본다.
    """
    try:
        data = json.loads(payload)
        blocks = data["content"]
    except (ValueError, KeyError, TypeError) as e:
        raise ModelCallError(f"응답 형식을 읽을 수 없음: {e}") from None
    if data.get("stop_reason") == "max_tokens":
        raise ModelCallError("응답이 max_tokens에서 잘림")
    text = "".join(
        b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    )
    if not text:
        raise ModelCallError("응답에 text 블록이 없음")
    return text


def make_complete(
    model: str,
    api_key: str,
    *,
    max_tokens: int = 1024,
    timeout: float = 60.0,
    post: Post = _post,
) -> Callable[[str], str]:
    """프롬프트 하나를 받아 모델 응답 텍스트를 돌려주는 함수를 만든다.

    model은 API의 model 문자열 그대로 넘긴다(예: 설정 파일의 값).
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }

    def complete(prompt: str) -> str:
        body = json.dumps(
            {
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return _text_of(post(API_URL, headers, body, timeout))

    return complete
