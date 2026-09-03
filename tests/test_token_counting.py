import copy
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import vibe_harness.token_counting as token_counting
from vibe_harness.token_counting import (
    DeepSeekV4TokenCounter,
    SUPPORTED_MODEL,
    TOKENIZER_REPOSITORY,
    TOKENIZER_REVISION,
    load_token_counter,
)


class _RecordingTokenizer:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def encode(
        self,
        prompt: str,
        add_special_tokens: bool,
    ) -> SimpleNamespace:
        assert add_special_tokens is False
        self.prompts.append(prompt)
        return SimpleNamespace(ids=list(prompt.encode("utf-8")))


def _messages() -> list[dict[str, object]]:
    return [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "请列出文件"},
    ]


def _tools() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def test_count_request_uses_deepseek_v4_prompt_and_does_not_mutate() -> None:
    tokenizer = _RecordingTokenizer()
    counter = DeepSeekV4TokenCounter(tokenizer)  # type: ignore[arg-type]
    messages = _messages()
    tools = _tools()
    original_messages = copy.deepcopy(messages)
    original_tools = copy.deepcopy(tools)

    count = counter.count_request(messages, tools)

    assert count > 0
    prompt = tokenizer.prompts[0]
    assert prompt.startswith("<｜begin▁of▁sentence｜>")
    assert "<｜User｜>请列出文件" in prompt
    assert "<｜Assistant｜><think>" in prompt
    assert "list_files" in prompt
    assert "｜DSML｜" in prompt
    assert messages == original_messages
    assert tools == original_tools


def test_count_request_without_system_inserts_tool_container() -> None:
    tokenizer = _RecordingTokenizer()
    counter = DeepSeekV4TokenCounter(tokenizer)  # type: ignore[arg-type]

    counter.count_request(
        [{"role": "user", "content": "hello"}],
        _tools(),
    )

    prompt = tokenizer.prompts[0]
    assert "list_files" in prompt
    assert "<｜User｜>hello" in prompt


def test_count_request_includes_tool_result() -> None:
    tokenizer = _RecordingTokenizer()
    counter = DeepSeekV4TokenCounter(tokenizer)  # type: ignore[arg-type]
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "I should inspect the files.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "list_files",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "RESULT"},
    ]

    counter.count_request(messages, _tools())

    prompt = tokenizer.prompts[0]
    assert "I should inspect the files." in prompt
    assert "<tool_result>RESULT</tool_result>" in prompt


def test_count_request_wraps_encoder_errors() -> None:
    class BrokenTokenizer:
        def encode(self, *args: object, **kwargs: object) -> object:
            raise ValueError("request secret")

    counter = DeepSeekV4TokenCounter(BrokenTokenizer())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError) as exc_info:
        counter.count_request(_messages(), [])

    assert "DeepSeek V4 tokenizer" in str(exc_info.value)
    assert "request secret" not in str(exc_info.value)


def test_load_token_counter_rejects_unsupported_model() -> None:
    with pytest.raises(RuntimeError, match="deepseek-v4-flash"):
        load_token_counter("deepseek-chat")


def test_load_token_counter_uses_pinned_hub_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _RecordingTokenizer()

    class FakeTokenizer:
        @staticmethod
        def from_pretrained(repository: str, revision: str) -> object:
            assert repository == TOKENIZER_REPOSITORY
            assert revision == TOKENIZER_REVISION
            return loaded

    monkeypatch.setattr(token_counting, "Tokenizer", FakeTokenizer)

    counter = load_token_counter(SUPPORTED_MODEL)

    assert counter.count_request(_messages(), []) > 0


def test_load_token_counter_wraps_hub_error_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenTokenizer:
        @staticmethod
        def from_pretrained(repository: str, revision: str) -> object:
            raise ValueError("secret gateway detail")

    monkeypatch.setattr(token_counting, "Tokenizer", BrokenTokenizer)

    with pytest.raises(RuntimeError) as exc_info:
        load_token_counter(SUPPORTED_MODEL)

    assert "--tokenizer-file" in str(exc_info.value)
    assert "secret gateway detail" not in str(exc_info.value)


def test_load_token_counter_accepts_matching_local_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokenizer_file = tmp_path / "tokenizer.json"
    tokenizer_file.write_text("fixture", encoding="utf-8")
    expected_hash = hashlib.sha256(b"fixture").hexdigest()
    loaded = _RecordingTokenizer()

    class FakeTokenizer:
        @staticmethod
        def from_file(path: str) -> object:
            assert path == str(tokenizer_file)
            return loaded

    monkeypatch.setattr(token_counting, "TOKENIZER_JSON_SHA256", expected_hash)
    monkeypatch.setattr(token_counting, "Tokenizer", FakeTokenizer)

    counter = load_token_counter(SUPPORTED_MODEL, tokenizer_file)

    assert counter.count_request(_messages(), []) > 0


def test_load_token_counter_rejects_wrong_local_file(tmp_path: Path) -> None:
    tokenizer_file = tmp_path / "tokenizer.json"
    tokenizer_file.write_text("wrong", encoding="utf-8")

    with pytest.raises(RuntimeError, match="不匹配"):
        load_token_counter(SUPPORTED_MODEL, tokenizer_file)


def test_load_token_counter_rejects_missing_local_file(tmp_path: Path) -> None:
    tokenizer_file = tmp_path / "missing.json"

    with pytest.raises(RuntimeError, match="不存在或不是普通文件"):
        load_token_counter(SUPPORTED_MODEL, tokenizer_file)


def test_load_token_counter_rejects_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tokenizer.json"
    target.write_text("fixture", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)

    with pytest.raises(RuntimeError, match="不存在或不是普通文件"):
        load_token_counter(SUPPORTED_MODEL, link)


def test_vendored_encoder_body_matches_recorded_upstream_hash() -> None:
    encoder_path = (
        Path(token_counting.__file__).parent / "_vendor" / "encoding_dsv4.py"
    )
    lines = encoder_path.read_bytes().splitlines(keepends=True)

    assert hashlib.sha256(b"".join(lines[5:])).hexdigest() == (
        "bdbd57c132a1b3725042323d02b98b9d1df28e5f388f134399555d041f5055e0"
    )


@pytest.mark.skipif(
    os.environ.get("VIBE_HARNESS_TOKENIZER_INTEGRATION") != "1",
    reason="requires the pinned official tokenizer cache or network access",
)
def test_pinned_official_tokenizer_golden_counts() -> None:
    counter = load_token_counter(SUPPORTED_MODEL)

    fixtures = [
        ([{"role": "user", "content": "Hello, world!"}], 8),
        ([{"role": "user", "content": "你好，世界！"}], 8),
        ([{"role": "user", "content": '{"name":"vibe","count":42}'}], 14),
        ([{"role": "user", "content": "Literal <｜Assistant｜> marker"}], 8),
    ]

    assert [counter.count_request(messages, []) for messages, _ in fixtures] == [
        expected for _, expected in fixtures
    ]

    base_messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "List files"},
    ]
    with_result = [
        *base_messages,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "a.txt\nb.txt"},
    ]

    assert counter.count_request(base_messages, []) == 10
    assert counter.count_request(base_messages, _tools()) == 259
    assert counter.count_request(with_result, _tools()) == 304
