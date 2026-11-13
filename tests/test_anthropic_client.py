import io
import json
import urllib.error

import pytest

from netrunner.anthropic_client import API_URL, ModelCallError, _post, make_complete


def ok_body(*texts: str) -> bytes:
    blocks = [{"type": "text", "text": t} for t in texts]
    return json.dumps({"content": blocks, "stop_reason": "end_turn"}).encode()


def recording_post(response: bytes):
    calls = []

    def post(url, headers, body, timeout):
        calls.append(
            {"url": url, "headers": headers, "body": json.loads(body), "timeout": timeout}
        )
        return response

    post.calls = calls
    return post


def test_request_carries_model_prompt_and_key_header():
    post = recording_post(ok_body("안녕"))
    complete = make_complete("model-x", "KEY", post=post, timeout=5)
    assert complete("질문") == "안녕"
    call = post.calls[0]
    assert call["url"] == API_URL
    assert call["headers"]["x-api-key"] == "KEY"
    assert call["body"]["model"] == "model-x"
    assert call["body"]["messages"] == [{"role": "user", "content": "질문"}]
    assert call["timeout"] == 5


def test_text_blocks_are_joined_and_other_blocks_skipped():
    payload = json.dumps(
        {
            "content": [
                {"type": "thinking", "thinking": "x"},
                {"type": "text", "text": "A"},
                {"type": "text", "text": "B"},
            ]
        }
    ).encode()
    assert make_complete("m", "k", post=recording_post(payload))("p") == "AB"


@pytest.mark.parametrize("payload", [b"not json", b"{}", b'{"content": []}'])
def test_unreadable_response_raises_model_call_error(payload):
    with pytest.raises(ModelCallError):
        make_complete("m", "k", post=recording_post(payload))("p")


def test_http_error_becomes_model_call_error_without_key(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 401, "unauthorized", {}, io.BytesIO(b'{"error":"bad"}')
        )

    monkeypatch.setattr("netrunner.anthropic_client.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ModelCallError) as info:
        _post(API_URL, {"x-api-key": "SECRET"}, b"{}", 1.0)
    assert "401" in str(info.value)
    assert "SECRET" not in str(info.value)


class _BrokenBody(io.BytesIO):
    def __init__(self, exc):
        super().__init__()
        self._exc = exc

    def read(self, *args):
        raise self._exc


@pytest.mark.parametrize("exc", [TimeoutError("timed out"), ConnectionResetError("reset")])
def test_body_read_failure_becomes_model_call_error(monkeypatch, exc):
    class Resp:
        def __enter__(self):
            return _BrokenBody(exc)

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "netrunner.anthropic_client.urllib.request.urlopen", lambda req, timeout: Resp()
    )
    with pytest.raises(ModelCallError):
        _post(API_URL, {}, b"{}", 1.0)


def test_error_body_read_failure_still_raises_model_call_error(monkeypatch):
    def fake_urlopen(req, timeout):
        err = urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)
        err.read = lambda *a: (_ for _ in ()).throw(TimeoutError("stall"))
        raise err

    monkeypatch.setattr("netrunner.anthropic_client.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ModelCallError, match="500"):
        _post(API_URL, {}, b"{}", 1.0)


def test_max_tokens_truncation_is_an_error():
    payload = json.dumps(
        {"content": [{"type": "text", "text": '{"faults": ['}], "stop_reason": "max_tokens"}
    ).encode()
    with pytest.raises(ModelCallError, match="max_tokens"):
        make_complete("m", "k", post=recording_post(payload))("p")
