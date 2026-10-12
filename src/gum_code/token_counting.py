"""Estimate model-visible request size without model-specific tokenizers."""

import json
import math
from typing import Protocol

BYTES_PER_TOKEN = 4


class RequestTokenCounter(Protocol):
    def count_request(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> int: ...


class EstimatedTokenCounter:
    def count_request(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> int:
        """Return a deterministic estimate for the complete visible request."""
        try:
            payload = json.dumps(
                {"messages": messages, "tools": tools},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise RuntimeError("无法估算模型请求 token") from None
        return max(1, (len(payload) + BYTES_PER_TOKEN - 1) // BYTES_PER_TOKEN)


def provider_prompt_tokens(response: object) -> int | None:
    """Return a self-consistent provider prompt count, or ignore the sample."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    values = [
        getattr(usage, name, None)
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
    ]
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in values
    ):
        return None
    prompt, completion, total = values
    if total < prompt + completion:
        return None
    return prompt


class TokenBudgetTracker:
    """Use one validated provider sample as an append-only estimate anchor."""

    def __init__(self, counter: RequestTokenCounter) -> None:
        self._counter = counter
        self._scale = 1.0
        self.recovered_limit: int | None = None
        self._anchor_local: int | None = None
        self._anchor_provider: int | None = None

    def count_request(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> int:
        local = self._counter.count_request(messages, tools)
        calibrated = math.ceil(local * self._scale)
        if self._anchor_local is None or self._anchor_provider is None:
            return calibrated
        if local < self._anchor_local:
            self.reset()
            return calibrated
        anchored = self._anchor_provider + max(0, local - self._anchor_local)
        return max(local, anchored)

    def local_count_request(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> int:
        return self._counter.count_request(messages, tools)

    def observe(
        self,
        response: object,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> None:
        prompt_tokens = provider_prompt_tokens(response)
        if prompt_tokens is None:
            return
        self._anchor_local = self._counter.count_request(messages, tools)
        self._anchor_provider = prompt_tokens
        self._scale = max(self._scale, prompt_tokens / max(1, self._anchor_local))

    def reset(self) -> None:
        """Discard the append-only anchor, retaining conservative model calibration."""
        self._anchor_local = None
        self._anchor_provider = None


def load_token_counter(model: str) -> EstimatedTokenCounter:
    """Create the model-neutral counter; the model name is accepted for API stability."""
    if not isinstance(model, str) or not model.strip():
        raise RuntimeError("model 必须是非空字符串")
    return EstimatedTokenCounter()
