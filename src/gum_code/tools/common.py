from pathlib import Path

SKIP_NAMES = {
    ".env",
    ".git",
    ".venv",
    ".aws",
    ".ssh",
    ".DS_Store",
    "__pycache__",
    ".pytest_cache",
    ".transcripts",
    ".task_outputs",
}

# Search noise is not necessarily private: compilers must be able to update caches.
SENSITIVE_PATH_NAMES = SKIP_NAMES - {"__pycache__", ".pytest_cache"}

SENSITIVE_FILE_NAMES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "id_rsa",
    "id_ed25519",
}

SENSITIVE_FILE_SUFFIXES = {
    ".key",
    ".p12",
    ".pfx",
    ".pem",
}

SAFE_ENV_TEMPLATE_NAMES = {
    ".env.example",
    ".env.sample",
    ".env.template",
}

MAX_OUTPUT_CHARS = 20_000


def _resolve_workspace_path(
    workspace: Path,
    path: str,
) -> Path:
    root = workspace.resolve()
    requested_path = Path(path)

    if requested_path.is_absolute():
        raise ValueError(f"只允许使用相对路径：{path!r}")

    target = (root / requested_path).resolve()

    if not target.is_relative_to(root):
        raise ValueError(f"路径越界：{path!r} 超出工作目录范围")

    relative_target = target.relative_to(root)

    if _is_sensitive_path(relative_target):
        raise ValueError("禁止访问敏感或内部路径")

    return target


def _is_sensitive_name(name: str) -> bool:
    if name in SAFE_ENV_TEMPLATE_NAMES:
        return False

    if name in SENSITIVE_PATH_NAMES or name in SENSITIVE_FILE_NAMES:
        return True

    if name.startswith(".env."):
        return True

    return Path(name).suffix.lower() in SENSITIVE_FILE_SUFFIXES


def _is_sensitive_path(relative: Path) -> bool:
    return any(_is_sensitive_name(part) for part in relative.parts)


def _sensitive_glob_patterns() -> tuple[str, ...]:
    names = sorted(SKIP_NAMES | SENSITIVE_FILE_NAMES)
    patterns: list[str] = []

    for name in names:
        patterns.extend((f"!{name}", f"!**/{name}", f"!**/{name}/**"))

    patterns.extend(("!.env.*", "!**/.env.*"))

    for suffix in sorted(SENSITIVE_FILE_SUFFIXES):
        patterns.extend((f"!*{suffix}", f"!**/*{suffix}"))

    return tuple(patterns)


def _reject_symlinks(workspace: Path, path: str) -> None:
    root = workspace.resolve()
    requested = Path(path)

    current = root
    for part in requested.parts:
        if part in (".", ".."):
            continue
        current = current / part
        if current.is_symlink():
            raise ValueError("禁止通过符号链接访问")


def _truncate_output(
    text: str,
    max_chars: int = MAX_OUTPUT_CHARS,
) -> str:
    if len(text) > max_chars:
        return text[:max_chars] + "\n[output truncated]"
    return text
