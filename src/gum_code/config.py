"""Load, validate, and privately persist provider configuration."""

import json
import io
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values
from openai import OpenAI

REQUIRED_CONFIG_KEYS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
CONFIG_KEYS = (*REQUIRED_CONFIG_KEYS, "LLM_CONTEXT_WINDOW")
BUILTIN_CONTEXT_WINDOWS = {"deepseek-v4-flash": 1_000_000}
CONTEXT_WINDOW_FIELDS = (
    "max_input_tokens",
    "max_input_context_length",
    "context_window",
    "context_length",
    "max_context_length",
    "max_model_len",
)
DEFAULT_CONTEXT_BUDGET_RATIO = 0.8
DEFAULT_VISION_CALL_LIMIT = 32
MAX_VISION_CONFIG_BYTES = 32_768


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    context_window: int | None = None


@dataclass(frozen=True)
class VisionSettings:
    api_key: str = field(repr=False)
    base_url: str
    model: str
    enabled: bool = False
    default_call_limit: int = DEFAULT_VISION_CALL_LIMIT


def default_user_config_path() -> Path:
    base = os.getenv("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "gum-code" / ".env"


def default_vision_config_path() -> Path:
    return default_user_config_path().with_name(".env.vision")


def validate_vision_settings(settings: VisionSettings) -> None:
    """Validate local input without probing a provider or displaying its values."""
    if not isinstance(settings, VisionSettings):
        raise ValueError("图片解析配置无效")
    for value, maximum in (
        (settings.api_key, 4096), (settings.base_url, 2048), (settings.model, 256),
    ):
        if (
            not isinstance(value, str) or not value.strip() or len(value) > maximum
            or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("图片解析配置字段无效")
    try:
        parsed = urlparse(settings.base_url)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or "?" in settings.base_url or "#" in settings.base_url
            or "\\" in settings.base_url or any(char.isspace() for char in settings.base_url)
        ):
            raise ValueError
        parsed.port
    except ValueError:
        raise ValueError("图片解析服务地址必须是无认证信息及查询参数的 HTTP(S) URL") from None
    if type(settings.enabled) is not bool:
        raise ValueError("图片解析启用状态无效")
    if type(settings.default_call_limit) is not int or settings.default_call_limit <= 0:
        raise ValueError("图片解析每轮上限必须是正整数")


def load_vision_settings(path: Path | None = None) -> VisionSettings | None:
    """Load only the private visual config; absent/invalid data disables vision."""
    target = default_vision_config_path() if path is None else Path(path)
    try:
        if any(part.is_symlink() for part in (target, *target.parents)):
            return None
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            return None
        owner = getuid()
        directory = target.parent.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != owner
                or stat.S_IMODE(directory.st_mode) != 0o700):
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(target, flags)
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_VISION_CONFIG_BYTES
                    or info.st_uid != owner or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600):
                return None
            data = file.read(MAX_VISION_CONFIG_BYTES + 1)
        if len(data) > MAX_VISION_CONFIG_BYTES:
            return None
        # No environment interpolation: persisted user consent cannot depend on .env.
        values = dotenv_values(stream=io.StringIO(data.decode("utf-8")), interpolate=False)
        enabled = values.get("VISION_ENABLED")
        if enabled not in {"true", "false"}:
            return None
        settings = VisionSettings(
            api_key=values.get("VISION_API_KEY"),
            base_url=values.get("VISION_BASE_URL"),
            model=values.get("VISION_MODEL"),
            enabled=enabled == "true",
            default_call_limit=int(values.get("VISION_DEFAULT_CALL_LIMIT", "")),
        )
        validate_vision_settings(settings)
        return settings
    except (OSError, UnicodeError, TypeError, ValueError):
        return None


def save_vision_settings(path: Path, settings: VisionSettings) -> None:
    """Save a host-confirmed configuration separately from the main provider."""
    validate_vision_settings(settings)
    target = Path(path)
    if any(part.is_symlink() for part in (target, *target.parents)):
        raise ValueError("图片解析配置路径无效")
    content = "".join(
        f"{name}={json.dumps(value, ensure_ascii=False)}\n"
        for name, value in (
            ("VISION_API_KEY", settings.api_key), ("VISION_BASE_URL", settings.base_url),
            ("VISION_MODEL", settings.model), ("VISION_ENABLED", str(settings.enabled).lower()),
            ("VISION_DEFAULT_CALL_LIMIT", str(settings.default_call_limit)),
        )
    )
    if len(content.encode("utf-8")) > MAX_VISION_CONFIG_BYTES:
        raise ValueError("图片解析配置超过大小上限")
    _save_private_config(target, content)


def _read_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("配置文件路径无效")
    values = dotenv_values(path)
    return {
        key: value
        for key, value in values.items()
        if key in CONFIG_KEYS and isinstance(value, str) and value.strip()
    }


def load_settings(
    user_config_path: Path | None = None,
    legacy_path: Path | None = None,
) -> Settings:
    """Resolve settings by environment, user config, then legacy-file precedence."""
    effective = {
        key: value.strip()
        for key in CONFIG_KEYS
        if isinstance((value := os.getenv(key)), str) and value.strip()
    }
    if len(effective) < len(CONFIG_KEYS):
        user_values = _read_values(
            default_user_config_path()
            if user_config_path is None
            else Path(user_config_path)
        )
        for key in CONFIG_KEYS:
            if key not in effective and key in user_values:
                effective[key] = user_values[key].strip()
    if len(effective) < len(CONFIG_KEYS):
        legacy_values = _read_values(
            Path.cwd() / ".env" if legacy_path is None else Path(legacy_path)
        )
        for key in CONFIG_KEYS:
            if key not in effective and key in legacy_values:
                effective[key] = legacy_values[key].strip()
    for key in REQUIRED_CONFIG_KEYS:
        if key not in effective:
            raise RuntimeError(f"{key} 未设置，请配置 GuMi-Code provider")

    context_window = None
    if "LLM_CONTEXT_WINDOW" in effective:
        try:
            context_window = int(effective["LLM_CONTEXT_WINDOW"])
        except ValueError:
            raise RuntimeError("LLM_CONTEXT_WINDOW 必须是正整数") from None

    settings = Settings(
        api_key=effective["LLM_API_KEY"],
        base_url=effective["LLM_BASE_URL"],
        model=effective["LLM_MODEL"],
        context_window=context_window,
    )
    validate_settings(settings)
    return settings


def legacy_public_defaults(path: Path) -> tuple[str, str]:
    values = _read_values(Path(path))
    return values.get("LLM_BASE_URL", ""), values.get("LLM_MODEL", "")


def has_complete_legacy_settings(path: Path) -> bool:
    try:
        values = _read_values(Path(path))
        if any(key not in values for key in REQUIRED_CONFIG_KEYS):
            return False
        validate_settings(
            Settings(
                values["LLM_API_KEY"],
                values["LLM_BASE_URL"],
                values["LLM_MODEL"],
                int(values["LLM_CONTEXT_WINDOW"])
                if "LLM_CONTEXT_WINDOW" in values
                else None,
            )
        )
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def validate_settings(settings: Settings) -> None:
    if not settings.api_key.strip():
        raise RuntimeError("LLM_API_KEY 未设置")
    parsed = urlparse(settings.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("LLM_BASE_URL 必须是有效的 HTTP(S) URL")
    if not settings.model.strip():
        raise RuntimeError("LLM_MODEL 未设置")
    if (
        settings.context_window is not None
        and (
            isinstance(settings.context_window, bool)
            or not isinstance(settings.context_window, int)
            or settings.context_window <= 0
        )
    ):
        raise RuntimeError("LLM_CONTEXT_WINDOW 必须是正整数")


def save_user_settings(path: Path, settings: Settings) -> None:
    """Validate and atomically replace a private provider settings file."""
    validate_settings(settings)
    content = "".join(
        (
            f"LLM_API_KEY={json.dumps(settings.api_key)}\n",
            f"LLM_BASE_URL={json.dumps(settings.base_url)}\n",
            f"LLM_MODEL={json.dumps(settings.model)}\n",
            *(
                (f"LLM_CONTEXT_WINDOW={settings.context_window}\n",)
                if settings.context_window is not None
                else ()
            ),
        )
    )
    _save_private_config(Path(path), content)


def _save_private_config(target: Path, content: str) -> None:
    if target.is_symlink():
        raise ValueError("配置文件不允许是符号链接")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(target.parent), prefix=".tmp-config-"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
            os.fchmod(file.fileno(), 0o600)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def validate_provider_settings(
    settings: Settings,
    client_factory: Callable[..., object] = OpenAI,
) -> None:
    validate_settings(settings)
    try:
        client = client_factory(
            api_key=settings.api_key,
            base_url=settings.base_url,
            max_retries=0,
            timeout=10,
        )
        client.chat.completions.create(
            model=settings.model,
            messages=[{"role": "user", "content": "Reply OK."}],
            max_tokens=1,
            timeout=10,
        )
    except KeyboardInterrupt:
        raise
    except Exception:
        raise RuntimeError("无法验证 provider 配置") from None


def _positive_context_window(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not value.isdigit():
            return None
        value = int(value)
    if isinstance(value, int) and value > 0:
        return value
    return None


def _provider_context_window(client: object, model: str) -> int | None:
    try:
        with_options = getattr(client, "with_options", None)
        metadata_client = (
            with_options(timeout=10, max_retries=0)
            if callable(with_options)
            else client
        )
        metadata = metadata_client.models.retrieve(model)  # type: ignore[attr-defined]
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return None

    extra = getattr(metadata, "model_extra", None)
    for field in CONTEXT_WINDOW_FIELDS:
        value = getattr(metadata, field, None)
        if value is None and isinstance(extra, dict):
            value = extra.get(field)
        if (parsed := _positive_context_window(value)) is not None:
            return parsed
    return None


def resolve_context_window(settings: Settings, client: object) -> int:
    """Resolve capacity without exposing provider metadata failures."""
    validate_settings(settings)
    if settings.context_window is not None:
        return settings.context_window
    discovered = _provider_context_window(client, settings.model)
    if discovered is not None:
        return discovered
    builtin = BUILTIN_CONTEXT_WINDOWS.get(settings.model)
    if builtin is not None:
        return builtin
    raise RuntimeError("无法确定模型上下文窗口，请设置 LLM_CONTEXT_WINDOW")


def resolve_context_budget(
    context_window: int,
    requested_tokens: int | None,
) -> int:
    """Return the 80% default or validate a caller-selected lower budget."""
    window = _positive_context_window(context_window)
    if window is None:
        raise RuntimeError("模型上下文窗口必须是正整数")
    if requested_tokens is None:
        return max(1, int(window * DEFAULT_CONTEXT_BUDGET_RATIO))
    requested = _positive_context_window(requested_tokens)
    if requested is None:
        raise RuntimeError("max_context_tokens 必须是正整数")
    if requested > window:
        raise RuntimeError(
            f"max_context_tokens 不能超过模型上下文窗口 {window}"
        )
    return requested
