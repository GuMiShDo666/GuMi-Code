# DeepSeek V4 Tokenizer Research

## Current Target

- Confirmed runtime model: `deepseek-v4-flash`.
- DeepSeek's current model page identifies the deployed Flash version as
  `DeepSeek-V4-Flash-0731` and documents a 1M context window.
- The former `deepseek-chat` alias was retired after 2026-07-24 and is not a
  supported target for this task.
- Reviewed official repository revision:
  `60d8d70770c6776ff598c94bb586a859a38244f1` (2026-09-03 snapshot).

## Official Encoding Findings

DeepSeek V4 does not use a normal Jinja chat template as its source of truth.
Its official model repository supplies a self-contained `encoding_dsv4.py`
that handles:

- system, user, assistant, and tool messages;
- thinking/non-thinking framing;
- tool schema injection;
- DSML Tool Calls and Tool Results;
- special-token ordering.

Therefore, tokenizing compact JSON or message contents alone is not the model
request count. The prompt must first be rendered through the V4 encoder and
then passed to the matching V4 tokenizer.

## Loader Choice

The official model repository contains `tokenizer.json`. Hugging Face
`tokenizers.Tokenizer` can load it from a local file or from a Hub model id and
returns the concrete token IDs. This avoids the much larger `transformers`
dependency and avoids `trust_remote_code=True`.

The official DeepSeek token-usage download was also inspected in a temporary
directory. It contains a roughly 6.4 MB tokenizer JSON, a tokenizer config, and
a short `transformers` demo. It is useful corroboration, but the current V4
model repository plus its dedicated V4 encoder is a clearer versioned source
for tool-aware chat counting.

## Accuracy Boundary

DeepSeek states that actual processed tokens are determined by the usage values
returned by the API. Local model-matched counting is suitable for preflight
compaction, but a gateway may add hidden framing or deploy a newer model behind
the same API name. The implementation should expose both local and returned
prompt counts without claiming guaranteed billing equality.

## Sources Reviewed 2026-09-03

- https://api-docs.deepseek.com/quick_start/pricing/
- https://api-docs.deepseek.com/quick_start/token_usage/
- https://api-docs.deepseek.com/updates/
- https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash
- https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/tree/main/encoding
- https://huggingface.co/docs/tokenizers/main/api/tokenizer
