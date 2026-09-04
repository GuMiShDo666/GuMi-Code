"""Count DeepSeek V4 requests against a pinned tokenizer contract."""

import copy
import hashlib
from pathlib import Path
from typing import Protocol

from tokenizers import Tokenizer

from gum_code._vendor.encoding_dsv4 import encode_messages

SUPPORTED_MODEL = "deepseek-v4-flash"
TOKENIZER_REPOSITORY = "deepseek-ai/DeepSeek-V4-Flash"
TOKENIZER_REVISION = "60d8d70770c6776ff598c94bb586a859a38244f1"
TOKENIZER_JSON_SHA256 = (
    "8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf"
)


class RequestTokenCounter(Protocol):
    def count_request(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> int: ...


class DeepSeekV4TokenCounter:
    def __init__(self, tokenizer: Tokenizer) -> None:
        self._tokenizer = tokenizer

    def count_request(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> int:
        """Count provider serialization without mutating request data."""
        encoded_messages = copy.deepcopy(messages)

        if tools:
            # Mirror DeepSeek's schema placement without mutating caller input.
            copied_tools = copy.deepcopy(tools)
            for message in encoded_messages:
                if message.get("role") == "system":
                    message["tools"] = copied_tools
                    break
            else:
                encoded_messages.insert(
                    0,
                    {"role": "system", "content": "", "tools": copied_tools},
                )

        try:
            prompt = encode_messages(
                encoded_messages,
                thinking_mode="thinking",
            )
            encoding = self._tokenizer.encode(
                prompt,
                add_special_tokens=False,
            )
        except Exception as error:
            raise RuntimeError(
                "无法使用 DeepSeek V4 tokenizer 计算请求 token"
            ) from error

        count = len(encoding.ids)
        if count <= 0:
            raise RuntimeError("DeepSeek V4 tokenizer 返回了无效 token 数")
        return count


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_token_counter(
    model: str,
    tokenizer_file: Path | None = None,
) -> DeepSeekV4TokenCounter:
    """Load only the pinned model tokenizer, verifying explicit files by hash."""
    if model != SUPPORTED_MODEL:
        raise RuntimeError(
            "当前 tokenizer 只支持模型 deepseek-v4-flash，"
            f"收到：{model!r}"
        )

    if tokenizer_file is not None:
        path = Path(tokenizer_file).expanduser()
        if path.is_symlink() or not path.exists() or not path.is_file():
            raise RuntimeError(
                f"tokenizer-file 不存在或不是普通文件：{path}"
            )
        if _sha256(path) != TOKENIZER_JSON_SHA256:
            raise RuntimeError(
                "tokenizer-file 与固定版本的 DeepSeek V4 tokenizer 不匹配"
            )
        try:
            tokenizer = Tokenizer.from_file(str(path))
        except Exception as error:
            raise RuntimeError("无法加载本地 DeepSeek V4 tokenizer") from error
    else:
        try:
            tokenizer = Tokenizer.from_pretrained(
                TOKENIZER_REPOSITORY,
                revision=TOKENIZER_REVISION,
            )
        except Exception as error:
            raise RuntimeError(
                "无法加载固定版本的 DeepSeek V4 tokenizer；"
                "请检查网络，或使用 --tokenizer-file 指定官方 tokenizer.json"
            ) from error

    return DeepSeekV4TokenCounter(tokenizer)
