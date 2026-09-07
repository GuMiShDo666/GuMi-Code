"""Manage private XDG workspace trust, recents, and the Session catalog."""

import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from gum_code.session import (
    MAX_SESSION_FILE_BYTES,
    SESSION_VERSION,
    SessionData,
    SessionLease,
    copy_session_log,
    delete_session_log,
    load_session,
    migrate_session_to_v3,
)

MAX_STATE_BYTES = 256 * 1024
_PRODUCT_DIR = "gum-code"
_LEGACY_PRODUCT_DIR = "gu-harness"
_LEGACY_REPOSITORY_DIR = "vibe-harness"
_REPOSITORY_DIR = "Gum-Code"
_MIGRATION_MARKER = ".legacy-data-migrated-v1"


@dataclass(frozen=True)
class UserState:
    trusted: tuple[str, ...] = ()
    recent: tuple[str, ...] = ()
    last_used: str | None = None
    legacy_migration_decided: bool = False


@dataclass(frozen=True)
class SessionEntry:
    path: Path
    workspace: Path
    title: str
    modified_at: float


def config_dir() -> Path:
    configured = os.getenv("XDG_CONFIG_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".config"
    return root / _PRODUCT_DIR


def data_dir() -> Path:
    configured = os.getenv("XDG_DATA_HOME")
    root = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local" / "share"
    )
    return root / _PRODUCT_DIR


def state_path() -> Path:
    return config_dir() / "state.json"


def sessions_dir() -> Path:
    return data_dir() / "sessions"


def _legacy_config_dir() -> Path:
    return config_dir().parent / _LEGACY_PRODUCT_DIR


def _legacy_data_dir() -> Path:
    return data_dir().parent / _LEGACY_PRODUCT_DIR


def _validate_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ValueError(f"{label}不允许是符号链接")
    if path.exists() and not path.is_dir():
        raise ValueError(f"{label}必须是目录")


def _validate_source_file(
    path: Path,
    label: str,
    *,
    max_bytes: int | None = None,
) -> bool:
    if path.is_symlink():
        raise ValueError(f"{label}不允许是符号链接")
    if not path.exists():
        return False
    if not path.is_file():
        raise ValueError(f"{label}必须是普通文件")
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise ValueError(f"{label}超过允许大小")
    return True


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("迁移目标目录不允许是符号链接")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("迁移目标目录无效")
    os.chmod(path, 0o700)


def _publish_private_file(path: Path, content: bytes) -> bool:
    if path.is_symlink():
        raise ValueError("迁移目标文件不允许是符号链接")
    if path.exists():
        if not path.is_file():
            raise ValueError("迁移目标必须是普通文件")
        return False

    _ensure_private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp-migration-"
    )
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
            os.fchmod(file.fileno(), 0o600)
        try:
            # A hard link publishes atomically without replacing a concurrent winner.
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _renamed_workspace(value: object) -> object:
    if not isinstance(value, str):
        return value
    old_path = Path(value)
    if not old_path.is_absolute() or old_path.exists():
        return value
    positions = [
        index
        for index, part in enumerate(old_path.parts)
        if part == _LEGACY_REPOSITORY_DIR
    ]
    if len(positions) != 1:
        return value
    parts = list(old_path.parts)
    parts[positions[0]] = _REPOSITORY_DIR
    new_path = Path(*parts)
    return str(new_path) if new_path.exists() else value


def _rewrite_state_paths(content: bytes) -> bytes:
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return content
    if not isinstance(data, dict):
        return content

    changed = False
    for field in ("trusted", "recent"):
        values = data.get(field)
        if not isinstance(values, list):
            continue
        rewritten = [_renamed_workspace(value) for value in values]
        if rewritten != values:
            data[field] = rewritten
            changed = True
    rewritten_last = _renamed_workspace(data.get("last_used"))
    if rewritten_last != data.get("last_used"):
        data["last_used"] = rewritten_last
        changed = True
    return (
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        if changed
        else content
    )


def _rewrite_session_workspace(content: bytes) -> bytes:
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return content
    if not isinstance(data, dict):
        return content
    workspace = data.get("workspace")
    rewritten = _renamed_workspace(workspace)
    if rewritten == workspace:
        return content
    data["workspace"] = rewritten
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def migrate_legacy_user_data() -> None:
    """Copy supported legacy files once without exposing their contents."""
    marker = config_dir() / _MIGRATION_MARKER
    if marker.is_symlink():
        raise ValueError("迁移标记不允许是符号链接")
    if marker.exists():
        if not marker.is_file():
            raise ValueError("迁移标记必须是普通文件")
        return

    legacy_config = _legacy_config_dir()
    legacy_data = _legacy_data_dir()
    legacy_sessions = legacy_data / "sessions"
    for path, label in (
        (legacy_config, "旧配置目录"),
        (legacy_data, "旧数据目录"),
        (legacy_sessions, "旧 Session 目录"),
        (config_dir(), "新配置目录"),
        (data_dir(), "新数据目录"),
    ):
        _validate_directory(path, label)

    legacy_env = legacy_config / ".env"
    legacy_state = legacy_config / "state.json"
    has_env = _validate_source_file(legacy_env, "旧配置文件")
    has_state = _validate_source_file(
        legacy_state, "旧状态文件", max_bytes=MAX_STATE_BYTES
    )
    session_sources: list[Path] = []
    if legacy_sessions.exists():
        session_sources = sorted(legacy_sessions.glob("*.json"))
        for source in session_sources:
            _validate_source_file(
                source, "旧 Session 文件", max_bytes=MAX_SESSION_FILE_BYTES
            )

    if has_env:
        _publish_private_file(config_dir() / ".env", legacy_env.read_bytes())
    if has_state:
        _publish_private_file(
            state_path(), _rewrite_state_paths(legacy_state.read_bytes())
        )
    if session_sources:
        destination_sessions = sessions_dir()
        _ensure_private_directory(destination_sessions)
        for source in session_sources:
            _publish_private_file(
                destination_sessions / source.name,
                _rewrite_session_workspace(source.read_bytes()),
            )

    _publish_private_file(marker, b"completed\n")


def _existing_directory(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.exists() or not path.is_dir():
        return None
    return str(path.resolve())


def load_user_state(path: Path | None = None) -> UserState:
    """Load valid existing workspace paths, treating malformed state as empty."""
    target = state_path() if path is None else Path(path)
    if not target.exists() or target.is_symlink() or not target.is_file():
        return UserState()
    try:
        if target.stat().st_size > MAX_STATE_BYTES:
            return UserState()
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return UserState()
    if not isinstance(raw, dict):
        return UserState()

    trusted_values = raw.get("trusted", [])
    recent_values = raw.get("recent", [])
    if not isinstance(trusted_values, list) or not isinstance(recent_values, list):
        return UserState()

    trusted = tuple(
        dict.fromkeys(
            path
            for value in trusted_values
            if (path := _existing_directory(value)) is not None
        )
    )
    recent = tuple(
        dict.fromkeys(
            path
            for value in recent_values
            if (path := _existing_directory(value)) is not None
        )
    )
    last_used = _existing_directory(raw.get("last_used"))
    return UserState(
        trusted=trusted,
        recent=recent,
        last_used=last_used,
        legacy_migration_decided=raw.get("legacy_migration_decided") is True,
    )


def _atomic_private_json(path: Path, value: dict[str, object]) -> None:
    if path.is_symlink():
        raise ValueError("用户状态文件不允许是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    content = json.dumps(value, ensure_ascii=False, indent=2)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp-state-"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
            os.fchmod(file.fileno(), 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save_user_state(state: UserState, path: Path | None = None) -> None:
    """Atomically persist user state in a private file."""
    target = state_path() if path is None else Path(path)
    _atomic_private_json(
        target,
        {
            "trusted": list(state.trusted),
            "recent": list(state.recent),
            "last_used": state.last_used,
            "legacy_migration_decided": state.legacy_migration_decided,
        },
    )


def record_workspace(state: UserState, workspace: Path, *, trusted: bool) -> UserState:
    resolved = str(Path(workspace).resolve())
    recent = (resolved, *(path for path in state.recent if path != resolved))
    trusted_paths = state.trusted
    if trusted and resolved not in trusted_paths:
        trusted_paths = (*trusted_paths, resolved)
    return UserState(
        trusted=trusted_paths,
        recent=recent,
        last_used=resolved,
        legacy_migration_decided=state.legacy_migration_decided,
    )


def remove_recent(state: UserState, workspace: Path) -> UserState:
    resolved = str(Path(workspace).resolve())
    return UserState(
        trusted=state.trusted,
        recent=tuple(path for path in state.recent if path != resolved),
        last_used=None if state.last_used == resolved else state.last_used,
        legacy_migration_decided=state.legacy_migration_decided,
    )


def revoke_trust(state: UserState, workspace: Path) -> UserState:
    resolved = str(Path(workspace).resolve())
    return UserState(
        trusted=tuple(path for path in state.trusted if path != resolved),
        recent=state.recent,
        last_used=state.last_used,
        legacy_migration_decided=state.legacy_migration_decided,
    )


def mark_legacy_migration_decided(state: UserState) -> UserState:
    return UserState(
        trusted=state.trusted,
        recent=state.recent,
        last_used=state.last_used,
        legacy_migration_decided=True,
    )


def workspace_creation_target(parent: Path, name: str) -> Path:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "\x00" in name
        or Path(name).name != name
        or "/" in name
        or (os.sep and os.sep in name)
        or (os.altsep and os.altsep in name)
    ):
        raise ValueError("新工作区名称必须是单个目录名")
    root = Path(parent).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("工作区父目录不存在")
    target = (root / name).resolve()
    if target.parent != root:
        raise ValueError("新工作区路径越界")
    return target


def create_workspace(parent: Path, name: str) -> Path:
    target = workspace_creation_target(parent, name)
    target.mkdir()
    return target


def allocate_session_path(directory: Path | None = None) -> Path:
    root = sessions_dir() if directory is None else Path(directory)
    if root.is_symlink():
        raise ValueError("Session 目录不允许是符号链接")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Session 目录路径无效")
    os.chmod(root, 0o700)
    while True:
        name = (
            datetime.now().strftime("%Y%m%d-%H%M%S-")
            + secrets.token_hex(4)
            + ".jsonl"
        )
        target = root / name
        if (
            not target.exists()
            and not target.with_name(target.name + ".lock").is_symlink()
        ):
            return target


def list_sessions(
    *,
    workspace: Path | None = None,
    directory: Path | None = None,
    limit: int | None = None,
) -> list[SessionEntry]:
    root = sessions_dir() if directory is None else Path(directory)
    if not root.exists() or root.is_symlink() or not root.is_dir():
        return []
    wanted = workspace.resolve() if workspace is not None else None
    loaded: list[tuple[Path, Path, str, float, int, Path | None]] = []
    paths = [*root.glob("*.jsonl"), *root.glob("*.json")]
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = load_session(
                path,
                allow_incomplete_tool_batch=True,
                allow_pending_user_tail=True,
            )
            loaded.append((path, data.workspace, data.title,
                           data.history_modified_at or path.stat().st_mtime,
                           data.version, data.legacy_source))
        except (OSError, ValueError):
            continue
    migrated_sources = {
        legacy_source.resolve()
        for *_, legacy_source in loaded
        if legacy_source is not None
    }
    entries: list[SessionEntry] = []
    for path, stored_workspace, title, modified_at, version, _ in loaded:
        if version != SESSION_VERSION and path.resolve() in migrated_sources:
            continue
        resolved_workspace = stored_workspace.resolve()
        if wanted is not None and resolved_workspace != wanted:
            continue
        entries.append(
            SessionEntry(
                path=path.resolve(),
                workspace=resolved_workspace,
                title=title or path.stem,
                modified_at=modified_at,
            )
        )
    entries.sort(key=lambda entry: entry.modified_at, reverse=True)
    return entries if limit is None else entries[:limit]


def import_session(
    source: Path,
    directory: Path | None = None,
    *,
    destination: Path | None = None,
) -> Path:
    source_path = Path(source).expanduser()
    if source_path.is_symlink():
        raise ValueError("不允许导入符号链接 Session")
    data: SessionData = load_session(
        source_path,
        allow_incomplete_tool_batch=True,
        allow_pending_user_tail=True,
    )
    target = (
        allocate_session_path(directory)
        if destination is None
        else Path(destination)
    )
    with SessionLease(target):
        if data.version != SESSION_VERSION:
            migrate_session_to_v3(source_path, target)
        else:
            copy_session_log(source_path, target)
    return target


def delete_session(path: Path) -> None:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError("Session 文件无效")
    delete_session_log(target)
