"""Decode a single JSON object from a model's diagnosis response."""

import json
import re

_OPEN_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n", re.IGNORECASE)
_CLOSE_FENCE = re.compile(r"```[ \t]*(?:\r?\n|$)")
_JSON_MARKUP = re.compile(r"[{}\[\]]|```")


def decode_response(text: str) -> dict:
    """Read a JSON object, allowing one Markdown fence and trailing prose."""
    text = text.strip()
    opening = _OPEN_FENCE.match(text)
    if opening:
        text = text[opening.end() :].lstrip()
    decoder = json.JSONDecoder()
    response, end = decoder.raw_decode(text)
    if not isinstance(response, dict):
        raise ValueError("응답은 JSON 객체여야 함")
    tail = text[end:].strip()
    if opening:
        closing = _CLOSE_FENCE.match(tail)
        if not closing:
            raise ValueError("JSON 코드 블록이 닫히지 않음")
        tail = tail[closing.end() :].strip()
    if tail:
        if _JSON_MARKUP.search(tail):
            raise ValueError("JSON 객체 뒤에 추가 구조가 있음")
        try:
            decoder.raw_decode(tail)
        except json.JSONDecodeError:
            pass
        else:
            raise ValueError("JSON 객체 뒤에 추가 값이 있음")
    return response
