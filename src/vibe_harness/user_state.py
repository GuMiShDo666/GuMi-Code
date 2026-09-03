import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vibe_harness.session import SessionData, SessionLease, load_session, save_session

MAX_STATE_BYTES = 256 * 1024


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
    return root / "gu-harness"


def data_dir() -> Path:
    configured = os.getenv("XDG_DATA_HOME")
    root = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local" / "share"
    )
    return root / "gu-harness"


def state_path() -> Path:
    return config_dir() / "state.json"


def sessions_dir() -> Path:
    return data_dir() / "sessions"


def _existing_directory(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.exists() or not path.is_dir():
        return None
    return str(path.resolve())


def load_user_state(path: Path | None = None) -> UserState:
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
            + ".json"
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
    entries: list[SessionEntry] = []
    for path in root.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = load_session(
                path,
                allow_incomplete_tool_batch=True,
                allow_pending_user_tail=True,
            )
            resolved_workspace = data.workspace.resolve()
            if wanted is not None and resolved_workspace != wanted:
                continue
            entries.append(
                SessionEntry(
                    path=path.resolve(),
                    workspace=resolved_workspace,
                    title=data.title or path.stem,
                    modified_at=path.stat().st_mtime,
                )
            )
        except (OSError, ValueError):
            continue
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
        save_session(
            target,
            data.workspace,
            data.model,
            data.messages,
            data.title,
        )
    return target


def delete_session(path: Path) -> None:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError("Session 文件无效")
    target.unlink()
