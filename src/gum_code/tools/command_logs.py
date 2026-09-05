"""Bounded command diagnostics and private, quota-limited completed logs."""

import codecs
import errno
import json
import os
import re
import secrets
import stat
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

COMMAND_LOG_DIR = Path(".task_outputs/tool-results/command-logs")
MAX_COMMAND_LOG_BYTES = 5 * 1024 * 1024
MAX_WORKSPACE_LOG_BYTES = 100 * 1024 * 1024
COMMAND_PREVIEW_BYTES = 8_192
_LOG_NAME = re.compile(r"command-[0-9a-f]{32}-(complete|truncated)\.txt\Z")
_LOG_HEADER = b"GUM_CODE_COMMAND_LOG_V1\n"
_OMITTED = "\n[... middle output omitted ...]\n"
_MARKER_START = "<command-output>\n"
_MARKER_END = "\n</command-output>"


class CapturedStream:
    """Keep at most one file budget per stream, independently of output volume."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total_bytes = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def add(self, chunk: bytes, *, final: bool = False) -> None:
        data = self._decoder.decode(chunk, final=final).encode("utf-8")
        self.total_bytes += len(data)
        half = MAX_COMMAND_LOG_BYTES // 2
        prefix = min(len(data), max(0, half - len(self.head)))
        self.head.extend(data[:prefix])
        self.tail.extend(data[prefix:])
        if len(self.tail) > half:
            del self.tail[: len(self.tail) - half]

    def render(self, budget: int) -> str:
        if self.total_bytes <= budget:
            return (self.head + self.tail).decode("utf-8")
        if budget <= 0:
            return _OMITTED
        half = budget // 2
        # Cuts can bisect a UTF-8 character; discard only those boundary bytes.
        head = bytes(self.head[:half]).decode("utf-8", errors="ignore")
        tail_size = budget - half
        missing = max(0, tail_size - len(self.tail))
        tail_bytes = (self.head[-missing:] if missing else bytearray()) + self.tail
        tail = bytes(tail_bytes[-tail_size:]).decode("utf-8", errors="ignore")
        return head + _OMITTED + tail


class CommandCapture:
    """Two fixed-size streams, with a shared serialization/preview budget."""

    def __init__(self) -> None:
        self.stdout = CapturedStream()
        self.stderr = CapturedStream()

    def render(self, status: str, budget: int) -> tuple[str, bool]:
        body_budget = max(0, budget - len(status.encode("utf-8")) - 256)
        stdout_budget = min(self.stdout.total_bytes, body_budget // 2)
        stderr_budget = min(self.stderr.total_bytes, body_budget - stdout_budget)
        stdout_budget += min(
            self.stdout.total_bytes - stdout_budget,
            body_budget - stdout_budget - stderr_budget,
        )
        truncated = (
            self.stdout.total_bytes > stdout_budget
            or self.stderr.total_bytes > stderr_budget
        )
        return (
            f"{status}\nstdout:\n{self.stdout.render(stdout_budget)}\n"
            f"stderr:\n{self.stderr.render(stderr_budget)}",
            truncated,
        )


def is_command_log_path(path: str) -> bool:
    requested = Path(path)
    return requested.parent == COMMAND_LOG_DIR and bool(_LOG_NAME.fullmatch(requested.name))


def _open_log_directory(workspace: Path, *, create: bool = False) -> int:
    """Walk using directory descriptors, never following internal symlinks."""
    if fcntl is None or os.name != "posix":
        raise OSError(errno.ENOSYS, "Private command log storage is unavailable")
    fd = os.open(workspace.resolve(), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in COMMAND_LOG_DIR.parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError("命令日志目录不允许符号链接或非目录路径") from None
                raise
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("命令日志目录必须由当前用户私有管理")
        return fd
    except BaseException:
        os.close(fd)
        raise


def prepare_command_logs(workspace: Path) -> tuple[int, int] | None:
    """Create the namespace before masking; ordinary storage errors allow capture."""
    try:
        fd = _open_log_directory(workspace, create=True)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        return info.st_dev, info.st_ino
    finally:
        os.close(fd)


def _private_file(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("命令日志不是当前用户的私有普通文件")
    return info


def _open_private_file(directory_fd: int, name: str, flags: int = os.O_RDONLY) -> int:
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENOTDIR):
            raise ValueError("命令日志文件不允许符号链接或非普通文件") from None
        raise
    try:
        _private_file(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def publish_command_log(
    workspace: Path,
    prepared: tuple[int, int] | None,
    content: str,
    truncated: bool,
) -> str | None:
    """Publish once under a nonblocking cross-process quota lock; never mkdir here."""
    # ponytail: publish at command end; stream to disk only if crash-time recovery is required.
    if prepared is None:
        return None
    data = _LOG_HEADER + content.encode("utf-8")
    if len(data) > MAX_COMMAND_LOG_BYTES:
        raise ValueError("命令日志超过保存上限")
    directory_fd = lock_fd = temporary_fd = None
    temporary_name = None
    try:
        directory_fd = _open_log_directory(workspace)
        info = os.fstat(directory_fd)
        if (info.st_dev, info.st_ino) != prepared:
            raise ValueError("命令日志目录在执行期间发生替换")
        lock_fd = _open_private_file(directory_fd, ".lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None

        total = 0
        completed: list[tuple[int, str, os.stat_result]] = []
        # Unknown files and abandoned temporary files count, but are never deleted.
        with os.scandir(directory_fd) as entries:
            for index, entry in enumerate(entries):
                if index >= 10_000:
                    return None
                name = entry.name
                info = entry.stat(follow_symlinks=False)
                total += info.st_size
                if not _LOG_NAME.fullmatch(name):
                    continue
                fd = _open_private_file(directory_fd, name)
                try:
                    info = os.fstat(fd)
                    if os.read(fd, len(_LOG_HEADER)) == _LOG_HEADER:
                        completed.append((info.st_mtime_ns, name, info))
                finally:
                    os.close(fd)
        for _, name, previous in sorted(completed):
            if total + len(data) <= MAX_WORKSPACE_LOG_BYTES:
                break
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
                current.st_mode, current.st_nlink,
            ) != (
                previous.st_dev, previous.st_ino, previous.st_size, previous.st_mtime_ns,
                previous.st_mode, previous.st_nlink,
            ):
                return None
            os.unlink(name, dir_fd=directory_fd)
            total -= previous.st_size
        if total + len(data) > MAX_WORKSPACE_LOG_BYTES:
            return None

        identity = secrets.token_hex(16)
        filename = f"command-{identity}-{'truncated' if truncated else 'complete'}.txt"
        candidate_name = f".tmp-{identity}"
        temporary_fd = _open_private_file(
            directory_fd, candidate_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        )
        temporary_name = candidate_name
        with os.fdopen(temporary_fd, "wb") as stream:
            temporary_fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # link is no-clobber; both names share one inode until temporary cleanup.
        os.link(temporary_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                follow_symlinks=False)
        os.unlink(temporary_name, dir_fd=directory_fd)
        temporary_name = None
        return str(COMMAND_LOG_DIR / filename)
    except OSError:
        return None
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_name is not None and directory_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
        if lock_fd is not None:
            os.close(lock_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def open_command_log(workspace: Path, path: str) -> int:
    """Return a validated readable descriptor; the caller owns closing it."""
    if not is_command_log_path(path):
        raise ValueError("命令日志路径无效")
    directory_fd = _open_log_directory(workspace)
    try:
        fd = _open_private_file(directory_fd, Path(path).name)
    finally:
        os.close(directory_fd)
    try:
        if os.fstat(fd).st_size > MAX_COMMAND_LOG_BYTES:
            raise ValueError("文件超过 5 MB 上限")
        if os.read(fd, len(_LOG_HEADER)) != _LOG_HEADER:
            raise ValueError("不是 Harness 生成的命令日志")
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise


def command_output_marker(metadata: dict[str, object], preview: str) -> str:
    return _MARKER_START + json.dumps(metadata, ensure_ascii=False) + "\nPreview:\n" + preview + _MARKER_END


def compact_command_output(content: str, workspace: Path, preview_chars: int) -> str | None:
    """Fit diagnostics without manufacturing a second purportedly complete log."""
    # Agent appends this fixed host reminder to the last result of a tool batch.
    reminder = "\n\n<reminder>Update your todos.</reminder>"
    suffix = reminder if content.endswith(reminder) else ""
    if suffix:
        content = content[:-len(suffix)]
    if not content.startswith(_MARKER_START) or not content.endswith(_MARKER_END):
        return None
    header, separator, preview = content[len(_MARKER_START):-len(_MARKER_END)].partition("\nPreview:\n")
    if not separator:
        return None
    try:
        metadata = json.loads(header)
    except ValueError:
        return None
    if not isinstance(metadata, dict) or set(metadata) != {
        "path", "log_status", "exit_code", "reason", "preview_truncated",
    }:
        return None
    path = metadata.get("path")
    if path is not None:
        if not isinstance(path, str) or not is_command_log_path(path):
            return None
        try:
            fd = open_command_log(workspace, path)
            os.close(fd)
        except (OSError, ValueError):
            metadata["log_status"] = "expired_or_unavailable"
    if len(preview) > preview_chars:
        if preview_chars:
            half = preview_chars // 2
            preview = preview[:half] + _OMITTED + preview[-(preview_chars - half):]
        else:
            preview = ""
        metadata["preview_truncated"] = True
    return command_output_marker(metadata, preview) + suffix
