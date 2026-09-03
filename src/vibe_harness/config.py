import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values
from openai import OpenAI

CONFIG_KEYS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str


def default_user_config_path() -> Path:
    base = os.getenv("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "gu-harness" / ".env"


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
    for key in CONFIG_KEYS:
        if key not in effective:
            raise RuntimeError(f"{key} 未设置，请配置 Gu-Harness provider")

    settings = Settings(
        api_key=effective["LLM_API_KEY"],
        base_url=effective["LLM_BASE_URL"],
        model=effective["LLM_MODEL"],
    )
    validate_settings(settings)
    return settings


def legacy_public_defaults(path: Path) -> tuple[str, str]:
    values = _read_values(Path(path))
    return values.get("LLM_BASE_URL", ""), values.get("LLM_MODEL", "")


def has_complete_legacy_settings(path: Path) -> bool:
    try:
        values = _read_values(Path(path))
        if any(key not in values for key in CONFIG_KEYS):
            return False
        validate_settings(
            Settings(
                values["LLM_API_KEY"],
                values["LLM_BASE_URL"],
                values["LLM_MODEL"],
            )
        )
        return True
    except (OSError, RuntimeError):
        return False


def validate_settings(settings: Settings) -> None:
    if not settings.api_key.strip():
        raise RuntimeError("LLM_API_KEY 未设置")
    parsed = urlparse(settings.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("LLM_BASE_URL 必须是有效的 HTTP(S) URL")
    if not settings.model.strip():
        raise RuntimeError("LLM_MODEL 未设置")


def save_user_settings(path: Path, settings: Settings) -> None:
    validate_settings(settings)
    target = Path(path)
    if target.is_symlink():
        raise ValueError("配置文件不允许是符号链接")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    content = "".join(
        (
            f"LLM_API_KEY={json.dumps(settings.api_key)}\n",
            f"LLM_BASE_URL={json.dumps(settings.base_url)}\n",
            f"LLM_MODEL={json.dumps(settings.model)}\n",
        )
    )
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
