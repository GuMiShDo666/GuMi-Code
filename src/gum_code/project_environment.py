"""Host-only project dependency preparation; checks reuse readonly owned volumes.

No source tree or user configuration enters an online preparation container.
The package lease remains held while the caller executes its checks.
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from gum_code.tools import command
from gum_code.tools.command import DependencyMount, DockerSandboxConfig
from gum_code.tools.command_logs import CommandCapture, _open_private_file
from gum_code.tools.common import _reject_symlinks, _resolve_workspace_path
from gum_code.user_state import data_dir

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_DEPENDENCIES = 512
PREPARE_TIMEOUT_SECONDS = 300
# Runtime services and short checks share the same immutable environment lease.
_ACTIVE_ENVIRONMENTS: dict[str, dict] = {}
# ponytail: serialize preparation; use per-package locks if parallel writers arrive.
_ACTIVE_LOCK = threading.RLock()
# Fixed official-image families; labels are discovery, immutable IDs are execution.
IMAGE_CANDIDATES = {
    "python": ("python:3.11-slim", "python:3.12-slim", "python:3.13-slim", "python:3.14-slim"),
    "node": ("node:22-slim", "node:24-slim"),
}
_PYTHON_FILES = ("requirements.txt", "requirements-dev.txt", "requirements-test.txt", "pyproject.toml")
_PYTHON_UNSUPPORTED = ("poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock", "pdm.lock")
_NODE_UNSUPPORTED = ("pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb", "npm-shrinkwrap.json", ".npmrc")
_PACKAGE_NAME = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*\Z")
_NPM_VERSION = re.compile(r"[A-Za-z0-9*^~<>=|. +_-]{1,200}\Z")
_PY_REQUIREMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,. -]+\])?(?:\s*\(?[!<>=~][A-Za-z0-9.*+!,<>=~ -]*\)?)?\s*(?:;[^\r\n]*)?\Z")


class _Unavailable(ValueError):
    """Carries a fixed diagnostic code, never an installer/provider exception."""

    def __init__(self, reason: str, *, cleanup_required: tuple[str, ...] = ()):
        super().__init__(reason)
        self.cleanup_required = cleanup_required


class EnvironmentInterrupted(KeyboardInterrupt):
    """Cancellation with exact host-generated resources whose cleanup is unknown."""

    def __init__(self, cleanup_required: tuple[str, ...]):
        super().__init__()
        self.cleanup_required = cleanup_required


@dataclass(frozen=True)
class ProjectSpec:
    root: str
    ecosystem: str
    identity: str
    reason: str = ""
    has_dependencies: bool = False
    runtime_constraint: str = ""
    installer_constraint: str = ""
    files: tuple[tuple[str, bytes], ...] = field(default=(), repr=False)
    dependencies: tuple[str, ...] = field(default=(), repr=False)
    package: bytes = field(default=b"", repr=False)
    lock: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class EnvironmentResult:
    status: str
    reason: str = ""
    sandbox: DockerSandboxConfig | None = None
    identity: str = ""
    excluded_dirs: tuple[str, ...] = ()
    cleanup_required: tuple[str, ...] = ()
    manifest_identity: str = ""


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _open_package(workspace: Path, root: str) -> int:
    _resolve_workspace_path(workspace, root)
    fd = os.open(workspace.resolve(), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(root).parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_manifest(workspace: Path, root: str, name: str) -> bytes | None:
    relative = str(Path(root) / name)
    _reject_symlinks(workspace, relative)
    path = _resolve_workspace_path(workspace, relative)
    directory = _open_package(workspace, root)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
    finally:
        os.close(directory)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_MANIFEST_BYTES:
            raise _Unavailable("manifest_unsafe")
        content = bytearray()
        while len(content) <= MAX_MANIFEST_BYTES:
            chunk = os.read(fd, min(65536, MAX_MANIFEST_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        stable = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if len(content) > MAX_MANIFEST_BYTES or stable(before) != stable(after) or stable(after) != stable(current):
            raise _Unavailable("manifest_changed")
        return bytes(content)
    finally:
        os.close(fd)


def _python_requirement(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise _Unavailable("unsupported_dependency")
    text = value.strip()
    if not _PY_REQUIREMENT.fullmatch(text) or any(item in text for item in ("@", ":", "/", "\\", "$", "\x00")):
        raise _Unavailable("unsupported_dependency")
    return text


def _npm_dependencies(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > MAX_DEPENDENCIES:
        raise _Unavailable("unsupported_dependency")
    result = {}
    for name, version in value.items():
        if not isinstance(name, str) or not _PACKAGE_NAME.fullmatch(name) or not isinstance(version, str) or not _NPM_VERSION.fullmatch(version):
            raise _Unavailable("unsupported_dependency")
        result[name] = version
    return result


def _python_dependencies(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_DEPENDENCIES:
        raise _Unavailable("invalid_manifest")
    return [_python_requirement(item) for item in value]


def _validated_lock(raw: bytes, package: dict[str, object]) -> bytes:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise _Unavailable("manifest_limit")
    try:
        lock = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise _Unavailable("invalid_lock") from None
    if not isinstance(lock, dict) or lock.get("lockfileVersion") not in (2, 3):
        raise _Unavailable("unsupported_lock")
    packages = lock.get("packages")
    if not isinstance(packages, dict) or len(packages) > 10000 or not isinstance(packages.get(""), dict):
        raise _Unavailable("invalid_lock")
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        if packages[""].get(key, {}) != package.get(key, {}):
            raise _Unavailable("lock_mismatch")
    for path, info in packages.items():
        if not isinstance(path, str) or (path and not path.startswith("node_modules/")) or any(part in {"..", "."} for part in path.split("/")) or "\\" in path or not isinstance(info, dict):
            raise _Unavailable("unsupported_lock")
    pending = [lock]
    nodes = 0
    while pending:
        value = pending.pop()
        nodes += 1
        if nodes > 100000:
            raise _Unavailable("manifest_limit")
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            if value.get("link") is True:
                raise _Unavailable("unsupported_dependency")
            for key, item in value.items():
                if key == "resolved":
                    if not isinstance(item, str):
                        raise _Unavailable("unsupported_dependency")
                    parsed = urlsplit(item)
                    if parsed.scheme != "https" or parsed.hostname != "registry.npmjs.org" or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.query or parsed.fragment:
                        raise _Unavailable("unsupported_dependency")
                if key == "version" and isinstance(item, str) and not _NPM_VERSION.fullmatch(item):
                    raise _Unavailable("unsupported_dependency")
                pending.append(item)
    # Only installer metadata enters the preparation container, not arbitrary
    # lockfile annotations or project root lifecycle code.
    fields = {"version", "resolved", "integrity", "dependencies", "optionalDependencies", "peerDependencies", "peerDependenciesMeta", "engines", "os", "cpu", "libc", "bin", "dev", "optional", "devOptional", "inBundle", "hasInstallScript", "hasShrinkwrap"}
    safe = {path: {key: item for key, item in info.items() if key in fields} for path, info in packages.items() if path}
    safe[""] = {key: value for key, value in package.items() if key in {"name", "version", "dependencies", "devDependencies", "optionalDependencies", "engines"}}
    return json.dumps({"lockfileVersion": 3, "requires": True, "packages": safe}, separators=(",", ":"), sort_keys=True).encode()


def discover_project(workspace: Path, package_root: str = ".", *, ecosystem: str) -> ProjectSpec:
    """Read only bounded native manifests; unsupported sources never get fetched."""
    files: list[tuple[str, bytes]] = []
    try:
        root = _resolve_workspace_path(workspace, package_root)
        if not root.is_dir() or ecosystem not in IMAGE_CANDIDATES:
            raise _Unavailable("unsupported_project")
        package_root = root.relative_to(workspace.resolve()).as_posix()
        unsupported = _PYTHON_UNSUPPORTED if ecosystem == "python" else _NODE_UNSUPPORTED
        if any((root / name).exists() or (root / name).is_symlink() for name in unsupported):
            raise _Unavailable("unsupported_package_manager")
        names = _PYTHON_FILES if ecosystem == "python" else ("package.json", "package-lock.json")
        for name in names:
            raw = _read_manifest(workspace, package_root, name)
            if raw is not None:
                files.append((name, raw))
        contents = dict(files)
        dependencies: list[str] = []
        constraint = installer_constraint = ""
        package = lock = b""
        if ecosystem == "python":
            for name in _PYTHON_FILES[:-1]:
                for line in contents.get(name, b"").decode("utf-8").splitlines():
                    line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
                    if line and not line.startswith("#"):
                        dependencies.append(_python_requirement(line))
            if "pyproject.toml" in contents:
                doc = tomllib.loads(contents["pyproject.toml"].decode("utf-8"))
                project = doc.get("project", {})
                if "poetry" in doc.get("tool", {}) or ("project" not in doc and "tool" in doc):
                    raise _Unavailable("unsupported_package_manager")
                if not isinstance(project.get("dynamic", []), list):
                    raise _Unavailable("invalid_manifest")
                if not isinstance(project, dict) or any(item in project.get("dynamic", []) for item in ("dependencies", "optional-dependencies")):
                    raise _Unavailable("dynamic_dependencies")
                dependencies.extend(_python_dependencies(project.get("dependencies", [])))
                optional = project.get("optional-dependencies", {})
                if not isinstance(optional, dict):
                    raise _Unavailable("invalid_manifest")
                for group in ("dev", "test", "tests"):
                    dependencies.extend(_python_dependencies(optional.get(group, [])))
                    dependencies.extend(_python_dependencies(doc.get("dependency-groups", {}).get(group, [])))
                dependencies.extend(_python_dependencies(doc.get("build-system", {}).get("requires", [])))
                constraint = project.get("requires-python", "")
                if not isinstance(constraint, str) or len(constraint) > 200 or not re.fullmatch(r"[0-9.!*<>=~, ]*", constraint):
                    raise _Unavailable("runtime_constraint_unsupported")
            dependencies = list(dict.fromkeys(dependencies))
            if len(dependencies) > MAX_DEPENDENCIES:
                raise _Unavailable("manifest_limit")
        else:
            target = root / "node_modules"
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise _Unavailable("dependency_target_unsafe")
            doc = json.loads(contents.get("package.json", b"{}"))
            if not isinstance(doc, dict) or doc.get("workspaces") or doc.get("overrides"):
                raise _Unavailable("unsupported_project")
            manager = doc.get("packageManager", "npm")
            if not isinstance(manager, str) or not (manager == "npm" or manager.startswith("npm@")):
                raise _Unavailable("unsupported_package_manager")
            minimal = {"name": "gum-code-preparation", "version": "0.0.0", "private": True}
            if isinstance(doc.get("name"), str) and _PACKAGE_NAME.fullmatch(doc["name"]):
                minimal["name"] = doc["name"]
            if isinstance(doc.get("version"), str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", doc["version"]):
                minimal["version"] = doc["version"]
            for key in ("dependencies", "devDependencies", "optionalDependencies"):
                minimal[key] = _npm_dependencies(doc.get(key, {}))
            engines = doc.get("engines", {})
            if not isinstance(engines, dict):
                raise _Unavailable("invalid_manifest")
            constraint = engines.get("node", "")
            if not isinstance(constraint, str) or len(constraint) > 200 or (constraint and not _NPM_VERSION.fullmatch(constraint)):
                raise _Unavailable("runtime_constraint_unsupported")
            if constraint:
                minimal["engines"] = {"node": constraint}
            installer_constraint = engines.get("npm", "")
            if manager.startswith("npm@"):
                pinned = manager[4:]
                if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pinned):
                    raise _Unavailable("unsupported_package_manager")
                installer_constraint = (installer_constraint + " " + pinned).strip()
            if not isinstance(installer_constraint, str) or len(installer_constraint) > 200 or (installer_constraint and not _NPM_VERSION.fullmatch(installer_constraint)):
                raise _Unavailable("runtime_constraint_unsupported")
            package = json.dumps(minimal, separators=(",", ":"), sort_keys=True).encode()
            if "package-lock.json" in contents:
                lock = _validated_lock(contents["package-lock.json"], minimal)
            dependencies = [key for group in ("dependencies", "devDependencies", "optionalDependencies") for key in minimal[group]]
        identity = _digest([str(workspace.resolve()), package_root, ecosystem, [(name, hashlib.sha256(raw).hexdigest()) for name, raw in files]])
        return ProjectSpec(package_root, ecosystem, identity, has_dependencies=bool(dependencies), runtime_constraint=constraint, installer_constraint=installer_constraint, files=tuple(files), dependencies=tuple(dependencies), package=package, lock=lock)
    except _Unavailable as exc:
        reason = str(exc)
    except (ValueError, TypeError, OSError, AttributeError, UnicodeError, RecursionError):
        reason = "invalid_manifest"
    return ProjectSpec(package_root, ecosystem, "", reason=reason)


def _cancel(event: threading.Event | None) -> None:
    if event is not None and event.is_set():
        raise KeyboardInterrupt


def _run_step(sandbox: DockerSandboxConfig, arguments: list[str], *, container_name: str | None = None, cancel_event=None, limit: int = 0) -> bytes:
    """Bounded streaming control/install output; no logs contain manifest bodies."""
    _cancel(cancel_event)
    capture = CommandCapture()
    process = None
    try:
        process = subprocess.Popen([str(sandbox.docker_path), *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        command._communicate_bounded(process, PREPARE_TIMEOUT_SECONDS, capture, cancel_event=cancel_event)
        if process.returncode != 0:
            raise _Unavailable("preparation_failed")
        if limit and capture.stdout.total_bytes > limit:
            raise _Unavailable("preparation_output_limit")
        return capture.stdout.render(limit).encode() if limit else b""
    except BaseException as exc:
        if process is not None:
            command._stop_process(process)
        cleanup = ()
        if container_name is not None and process is not None:
            if not _cleanup_container(sandbox, container_name):
                cleanup = (container_name,)
        if isinstance(exc, KeyboardInterrupt):
            if cleanup:
                raise EnvironmentInterrupted(cleanup) from None
            raise
        if cleanup:
            raise _Unavailable("preparation_cleanup_required", cleanup_required=cleanup) from None
        if isinstance(exc, _Unavailable):
            raise
        raise _Unavailable("preparation_timeout" if isinstance(exc, subprocess.TimeoutExpired) else "preparation_failed") from None


def _container_arguments(sandbox: DockerSandboxConfig, argv: list[str], *, name: str, volume: str | None = None, ecosystem: str = "python", metadata: Path | None = None, online: bool = False, initialize: bool = False) -> list[str]:
    target = "/opt/gum-env" if ecosystem == "python" else "/prepare/node_modules"
    args = ["run", "--rm", "--name", name, "--pull=never", "--log-driver=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges=true", "--pids-limit", "128", "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--ulimit", "nofile=256:256", "--ipc=none", "--init", "--user", "0:0" if initialize else command._numeric_user(), "--network=bridge" if online else "--network=none", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777", "--tmpfs", "/prepare:rw,nosuid,nodev,size=32m,mode=1777", "--workdir", "/prepare", "--env", "HOME=/tmp", "--env", "XDG_CONFIG_HOME=/tmp", "--env", "XDG_CACHE_HOME=/tmp", "--env", "PIP_CONFIG_FILE=/dev/null", "--env", "PIP_DISABLE_PIP_VERSION_CHECK=1", "--env", "npm_config_userconfig=/tmp/gum-code-user.npmrc", "--env", "npm_config_globalconfig=/tmp/gum-code-global.npmrc", "--env", "npm_config_cache=/tmp/npm-cache", "--env", "npm_config_registry=https://registry.npmjs.org", "--env", "npm_config_audit=false", "--env", "npm_config_fund=false"]
    if initialize:
        args.extend(["--cap-add=CHOWN"])
    if volume:
        args.extend(["--mount", f"type=volume,source={volume},target={target},volume-nocopy"])
    if metadata:
        command._validate_mount_component(str(metadata))
        args.extend(["--mount", f"type=bind,source={metadata},target=/metadata,readonly"])
    return [*args, "--entrypoint", argv[0], sandbox.image_id, *argv[1:]]


def _container_step(sandbox, argv, *, cancel_event=None, limit=0, **options):
    name = f"gum-code-prepare-{secrets.token_hex(16)}"
    return _run_step(sandbox, _container_arguments(sandbox, argv, name=name, **options), container_name=name, cancel_event=cancel_event, limit=limit)


_PYTHON_PROBE = "import json,platform,pip,sys;from pip._vendor.packaging.specifiers import SpecifierSet;print(json.dumps({'runtime':platform.python_version(),'installer':pip.__version__,'compatible':platform.python_version() in SpecifierSet(sys.argv[1])}))"
_NODE_PROBE = "const r=require('module').createRequire('/usr/local/lib/node_modules/npm/package.json'),v=r('./package.json').version,s=r('semver');console.log(JSON.stringify({runtime:process.versions.node,installer:v,compatible:(!process.argv[1]||s.satisfies(process.versions.node,process.argv[1]))&&(!process.argv[2]||s.satisfies(v,process.argv[2]))}));"


def _probe(sandbox, project, cancel_event):
    argv = ["python", "-I", "-c", _PYTHON_PROBE, project.runtime_constraint] if project.ecosystem == "python" else ["node", "-e", _NODE_PROBE, project.runtime_constraint, project.installer_constraint]
    try:
        result = json.loads(_container_step(sandbox, argv, ecosystem=project.ecosystem, cancel_event=cancel_event, limit=16384))
    except _Unavailable:
        raise
    except (ValueError, UnicodeError):
        raise _Unavailable("runtime_unavailable") from None
    if not isinstance(result, dict) or result.get("compatible") is not True or any(not isinstance(result.get(key), str) or not re.fullmatch(r"[0-9][0-9A-Za-z.+-]{0,63}", result[key]) for key in ("runtime", "installer")):
        raise _Unavailable("runtime_incompatible")
    return result


def _candidate_images(project: ProjectSpec) -> tuple[str, ...]:
    """Conservative numeric range admission; installer checks the actual patch."""
    # ponytail: shortlist fixed image families here; the existing installer owns
    # full version-range semantics inside the actual selected runtime.
    constraint = project.runtime_constraint.strip()
    if not constraint or (project.ecosystem == "node" and constraint == "*"):
        return IMAGE_CANDIDATES[project.ecosystem]
    def fits(reference):
        version = reference.split(":")[1].split("-")[0]
        current = tuple(int(x) for x in version.split("."))
        low = (*current, *([0] * (3 - len(current))))
        high = (*current, *([999999] * (3 - len(current))))
        alternatives = constraint.split("||") if project.ecosystem == "node" else [constraint]
        for alternative in alternatives:
            tokens = re.findall(r"(>=|<=|==|!=|~=|>|<|=|\^|~)?\s*(\d+(?:\.\d+){0,2})(\.\*)?", alternative)
            consumed = re.sub(r"(>=|<=|==|!=|~=|>|<|=|\^|~)?\s*\d+(?:\.\d+){0,2}(?:\.\*)?", "", alternative)
            if not tokens or consumed.strip(" ,"):
                raise _Unavailable("runtime_constraint_unsupported")
            ok = True
            for operator, number, wildcard in tokens:
                parts = tuple(map(int, number.split(".")))
                wanted = (*parts, *([0] * (3 - len(parts))))
                if operator in ("", "=", "=="):
                    upper = (*parts, *([999999] * (3 - len(parts))))
                    valid = low <= upper and high >= wanted
                elif operator == "!=":
                    valid = not (wildcard and low[:len(parts)] == parts and high[:len(parts)] == parts)
                elif operator in ("^", "~", "~="):
                    prefix = 1 if operator == "^" else max(1, len(parts) - 1)
                    valid = high >= wanted and low[:prefix] <= wanted[:prefix] <= high[:prefix]
                else:
                    valid = {">": high > wanted, ">=": high >= wanted, "<": low < wanted, "<=": low <= wanted}[operator]
                ok = ok and valid
            if ok:
                return True
        return False
    choices = tuple(image for image in IMAGE_CANDIDATES[project.ecosystem] if fits(image))
    if not choices:
        raise _Unavailable("runtime_incompatible")
    return choices


@contextmanager
def _lease(workspace: Path, scope: str, *, namespace: str = "project-environments"):
    if fcntl is None or os.name != "posix":
        raise _Unavailable("environment_storage_unavailable")
    directory = data_dir().absolute() / namespace
    if directory.is_relative_to(workspace.resolve()):
        raise _Unavailable("environment_storage_unsafe")
    # No source/config data is stored here; descriptors pin the private namespace.
    fd = os.open(directory.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in directory.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    lock = None
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise _Unavailable("environment_storage_unsafe")
        lock = _open_private_file(fd, scope + ".lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _Unavailable("environment_busy") from None
        yield fd
    finally:
        if lock is not None:
            os.close(lock)
        os.close(fd)


def _load_record(fd: int, scope: str) -> dict:
    try:
        record_fd = _open_private_file(fd, scope + ".json")
    except FileNotFoundError:
        return {}
    try:
        raw = os.read(record_fd, 8193)
        if len(raw) > 8192:
            raise _Unavailable("environment_storage_unsafe")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("version") != 1:
            raise _Unavailable("environment_storage_unsafe")
        for key in ("ready", "staging"):
            item = value.get(key)
            if item is not None and (not isinstance(item, dict) or not isinstance(item.get("volume"), str) or not command.ENVIRONMENT_VOLUME_RE.fullmatch(item["volume"])):
                raise _Unavailable("environment_storage_unsafe")
            if item is not None:
                names = item.get("containers", [])
                if not isinstance(names, list) or len(names) > 2 or any(not isinstance(name, str) or not re.fullmatch(r"gum-code-prepare-[0-9a-f]{32}", name) for name in names):
                    raise _Unavailable("environment_storage_unsafe")
        ready = value.get("ready")
        if ready is not None and (any(not isinstance(ready.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", ready[key]) for key in ("identity", "manifest")) or not isinstance(ready.get("image_id"), str) or not command._DOCKER_IMAGE_ID_RE.fullmatch(ready["image_id"])):
            raise _Unavailable("environment_storage_unsafe")
        return value
    finally:
        os.close(record_fd)


def _save_record(fd: int, scope: str, value: dict) -> None:
    name = f".pending-{secrets.token_hex(16)}"
    temporary = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(temporary, "wb") as file:
            file.write(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, scope + ".json", src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass


def _owned_volume(sandbox, volume, scope) -> bool:
    if not command.ENVIRONMENT_VOLUME_RE.fullmatch(volume):
        return False
    result = command._run_docker_control(sandbox.docker_path, ["volume", "inspect", "--format", "{{json .Labels}}", volume])
    try:
        labels = json.loads(result.stdout) if len(result.stdout) <= 8192 else None
    except (ValueError, UnicodeError):
        return False
    return result.returncode == 0 and isinstance(labels, dict) and labels.get(command.ENVIRONMENT_LABEL) == "v1" and labels.get(command.ENVIRONMENT_SCOPE_LABEL) == scope


def _absent_resource(sandbox, kind: str, name: str) -> bool:
    """Confirm an exact owned name disappeared; never enumerate or prune broadly."""
    try:
        field = "{{.Names}}" if kind == "container" else "{{.Name}}"
        arguments = [kind, "ls", *(["--all"] if kind == "container" else []), "--filter", f"name={name}", "--format", field]
        result = command._run_docker_control(sandbox.docker_path, arguments)
        return result.returncode == 0 and len(result.stdout) <= 8192 and name not in result.stdout.decode("utf-8").splitlines()
    except (ValueError, OSError, UnicodeError):
        return False


def _cleanup_container(sandbox, name: str) -> bool:
    if not re.fullmatch(r"gum-code-prepare-[0-9a-f]{32}", name):
        return False
    return command._remove_container(sandbox, name) or _absent_resource(sandbox, "container", name)


def _remove_owned(sandbox, volume, scope) -> bool:
    try:
        if not _owned_volume(sandbox, volume, scope):
            return _absent_resource(sandbox, "volume", volume)
        result = command._run_docker_control(sandbox.docker_path, ["volume", "rm", volume])
        return result.returncode == 0
    except ValueError:
        return False


def _fresh(workspace, project):
    current = discover_project(workspace, project.root, ecosystem=project.ecosystem)
    if current.reason or current.identity != project.identity:
        raise _Unavailable("manifest_changed")
    return current


def _write_generated_lock(workspace, project, raw, change_callback):
    _fresh(workspace, project)
    root = _resolve_workspace_path(workspace, project.root)
    fd = _open_package(workspace, project.root)
    name = f".gum-code-lock-{secrets.token_hex(16)}"
    created = False
    try:
        temporary = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        created = True
        with os.fdopen(temporary, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        _fresh(workspace, project)
        current = root.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
            raise _Unavailable("manifest_changed")
        # Missing-lock publication must never overwrite a concurrent user lock.
        os.link(name, "package-lock.json", src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.unlink(name, dir_fd=fd)
        created = False
        os.fsync(fd)
    finally:
        if created:
            os.unlink(name, dir_fd=fd)
        os.close(fd)
    if change_callback is not None:
        change_callback(str(Path(project.root) / "package-lock.json"))
    return discover_project(workspace, project.root, ecosystem=project.ecosystem)


def _prepare(sandbox, project, volume, workspace, cancel_event, change_callback):
    target = "/opt/gum-env" if project.ecosystem == "python" else "/prepare/node_modules"
    _container_step(sandbox, ["chown", command._numeric_user(), target], volume=volume, ecosystem=project.ecosystem, initialize=True, cancel_event=cancel_event)
    # A fixed OS temp parent ignores a project-controlled TMPDIR.
    temp_parent = Path("/tmp").resolve()
    if temp_parent.is_relative_to(workspace.resolve()):
        raise _Unavailable("environment_storage_unsafe")
    with tempfile.TemporaryDirectory(prefix="gum-code-environment-", dir=temp_parent) as directory:
        # Outer directory is private on the host; the mounted child must also
        # be readable by the fixed non-root fallback when the host uid is root.
        metadata = Path(directory) / "input"
        metadata.mkdir(mode=0o755)
        if project.ecosystem == "python":
            (metadata / "requirements.txt").write_text("\n".join(project.dependencies) + "\n", encoding="utf-8")
            os.chmod(metadata / "requirements.txt", 0o644)
            _container_step(sandbox, ["python", "-I", "-m", "venv", "/opt/gum-env"], volume=volume, cancel_event=cancel_event)
            _container_step(sandbox, ["/opt/gum-env/bin/python", "-I", "-m", "pip", "--isolated", "install", "--no-cache-dir", "--index-url", "https://pypi.org/simple", "--retries", "0", "-r", "/metadata/requirements.txt"], volume=volume, metadata=metadata, online=True, cancel_event=cancel_event)
            _container_step(sandbox, ["/opt/gum-env/bin/python", "-I", "-m", "pip", "check"], volume=volume, cancel_event=cancel_event)
        else:
            (metadata / "package.json").write_bytes(project.package)
            os.chmod(metadata / "package.json", 0o644)
            if not project.lock:
                script = "const fs=require('fs'),cp=require('child_process');fs.copyFileSync('/metadata/package.json','package.json');cp.execFileSync('npm',['install','--package-lock-only','--ignore-scripts','--include=dev','--registry=https://registry.npmjs.org','--no-audit','--no-fund'],{stdio:['ignore','ignore','inherit']});const b=fs.readFileSync('package-lock.json');if(b.length>2097152)process.exit(2);process.stdout.write(b);"
                raw = _container_step(sandbox, ["node", "-e", script], metadata=metadata, ecosystem="node", online=True, cancel_event=cancel_event, limit=MAX_MANIFEST_BYTES)
                raw = _validated_lock(raw, json.loads(project.package))
                project = _write_generated_lock(workspace, project, raw, change_callback)
                if project.reason:
                    raise _Unavailable(project.reason)
            (metadata / "package-lock.json").write_bytes(project.lock)
            os.chmod(metadata / "package-lock.json", 0o644)
            script = "const fs=require('fs'),cp=require('child_process');for(const n of ['package.json','package-lock.json'])fs.copyFileSync('/metadata/'+n,n);cp.execFileSync('npm',['ci','--include=dev','--registry=https://registry.npmjs.org','--no-audit','--no-fund'],{stdio:'inherit'});"
            _container_step(sandbox, ["node", "-e", script], volume=volume, metadata=metadata, ecosystem="node", online=True, cancel_event=cancel_event)
        return _fresh(workspace, project)


@contextmanager
def ensure_environment(workspace: Path, project: ProjectSpec, *, command_sandbox: DockerSandboxConfig | None = None, approval_callback: Callable[[dict], bool] | None = None, denied_scopes: set[str] | None = None, cancel_event: threading.Event | None = None, change_callback: Callable[[str], None] | None = None) -> Iterator[EnvironmentResult]:
    """Select/prepare/use under one package lease; consent never comes from disk.

    The caller must execute every check inside this context. Failures before the
    yield are fixed reasons; caller errors and cancellation propagate unchanged.
    """
    _cancel(cancel_event)
    if project.reason:
        yield EnvironmentResult("unavailable", project.reason)
        return
    scope = _digest([str(workspace.resolve()), project.root])
    entered = False
    active = None
    try:
        with _ACTIVE_LOCK:
            active = _ACTIVE_ENVIRONMENTS.get(scope)
            if active is not None:
                result = active["result"]
                current = _fresh(workspace, project)
                if current.identity != active["manifest"] or (command_sandbox is not None and (command_sandbox.image_id != result.sandbox.image_id or command_sandbox.docker_path != result.sandbox.docker_path)):
                    active = None
                    raise _Unavailable("environment_in_use")
                command._validate_docker_sandbox(result.sandbox)
                command._validate_dependency_mount(result.sandbox, workspace)
                active["users"] += 1
            else:
                lease = _lease(workspace, scope)
                fd = lease.__enter__()
                try:
                    result = _ensure_locked(workspace, project, scope, fd, command_sandbox, approval_callback, denied_scopes, cancel_event, change_callback)
                    if result.status == "ready":
                        current = discover_project(workspace, project.root, ecosystem=project.ecosystem)
                        if current.reason or current.identity != result.manifest_identity:
                            raise _Unavailable("manifest_changed")
                        active = {"result": result, "manifest": current.identity, "lease": lease, "users": 1}
                        _ACTIVE_ENVIRONMENTS[scope] = active
                    else:
                        lease.__exit__(None, None, None)
                except BaseException:
                    lease.__exit__(None, None, None)
                    raise
        entered = True
        yield result
    except (ValueError, OSError, TypeError) as exc:
        if entered:
            raise
        yield EnvironmentResult("unavailable", str(exc) if isinstance(exc, _Unavailable) else "environment_unavailable", cleanup_required=exc.cleanup_required if isinstance(exc, _Unavailable) else ())
    finally:
        if active is not None and entered:
            with _ACTIVE_LOCK:
                active["users"] -= 1
                if active["users"] == 0:
                    _ACTIVE_ENVIRONMENTS.pop(scope, None)
                    active["lease"].__exit__(None, None, None)


def _ensure_locked(workspace, project, scope, fd, explicit, approval, denied, cancel_event, change_callback):
    project = _fresh(workspace, project)
    candidates = _candidate_images(project) if explicit is None else IMAGE_CANDIDATES[project.ecosystem]
    record = _load_record(fd, scope)
    docker = explicit.docker_path if explicit is not None else Path(shutil.which("docker") or "/nonexistent")
    staging = record.get("staging")
    if staging and staging.get("containers"):
        control = DockerSandboxConfig(docker, "sha256:" + "0" * 64)
        pending = tuple(name for name in staging["containers"] if not _cleanup_container(control, name))
        if pending or not _remove_owned(control, staging["volume"], scope):
            return EnvironmentResult("unavailable", "preparation_cleanup_required", cleanup_required=pending + (staging["volume"],))
        record = {**record, "staging": None}
        _save_record(fd, scope, record)
    sandbox = None
    runtime = None
    missing = "docker_unavailable"
    if docker.is_file():
        try:
            version = command._run_docker_control(docker, ["version", "--format", "{{.Server.Version}}"])
        except ValueError:
            version = None
        if version is not None and version.returncode == 0 and version.stdout.strip():
            missing = "runtime_unavailable"
            old = record.get("ready") or {}
            choices = [explicit.image_id] if explicit else ([old["image_id"]] if old.get("manifest") == project.identity and isinstance(old.get("image_id"), str) else []) + list(candidates)
            for image in choices:
                _cancel(cancel_event)
                try:
                    sandbox = DockerSandboxConfig(docker, command._inspect_local_image(docker, image))
                    runtime = _probe(sandbox, project, cancel_event)
                    break
                except ValueError as exc:
                    if isinstance(exc, _Unavailable) and exc.cleanup_required:
                        raise
                    sandbox = None
    identity = _digest([project.identity, sandbox.image_id, runtime]) if sandbox else ""
    _fresh(workspace, project)
    ready = record.get("ready")
    if sandbox and ready and ready.get("identity") == identity and _owned_volume(sandbox, ready["volume"], scope):
        return _environment_result(workspace, project, sandbox, scope, ready["volume"], identity)
    if sandbox and not project.has_dependencies:
        return EnvironmentResult("ready", sandbox=sandbox, identity=identity, manifest_identity=project.identity)
    consent_scope = _digest([project.identity, sandbox.image_id if sandbox else candidates[0], runtime, missing])
    if denied is not None and consent_scope in denied:
        return EnvironmentResult("skipped", "user_declined")
    if approval is None:
        return EnvironmentResult("unavailable", "needs_input")
    summary = {"missing": "project_dependencies" if sandbox else missing, "ecosystem": project.ecosystem, "runtime": runtime["runtime"] if runtime else candidates[0], "public_source": "pypi.org" if project.ecosystem == "python" else "registry.npmjs.org", "downloads": True, "disk_usage": True, "dependency_scripts": True}
    _cancel(cancel_event)
    try:
        approved = approval(summary)
    except EOFError:
        return EnvironmentResult("unavailable", "needs_input")
    if approved is not True:
        if denied is not None:
            denied.add(consent_scope)
        return EnvironmentResult("skipped", "user_declined")
    _cancel(cancel_event)
    _fresh(workspace, project)
    if missing == "docker_unavailable":
        return EnvironmentResult("unavailable", "docker_unavailable")
    if sandbox is None:
        if explicit is not None:
            return EnvironmentResult("unavailable", "runtime_incompatible")
        sandbox = DockerSandboxConfig(docker, "sha256:" + "0" * 64)
        _run_step(sandbox, ["pull", candidates[0]], cancel_event=cancel_event)
        sandbox = DockerSandboxConfig(docker, command._inspect_local_image(docker, candidates[0]))
        runtime = _probe(sandbox, project, cancel_event)
    if not project.has_dependencies:
        return EnvironmentResult("ready", sandbox=sandbox, identity=_digest([project.identity, sandbox.image_id, runtime]), manifest_identity=project.identity)
    staging = record.get("staging")
    if staging and not _remove_owned(sandbox, staging["volume"], scope):
        return EnvironmentResult("unavailable", "environment_cleanup_required", cleanup_required=(staging["volume"],))
    volume = "gum-code-env-" + secrets.token_hex(16)
    record = {"version": 1, "ready": ready, "staging": {"volume": volume}}
    published = False
    _save_record(fd, scope, record)
    try:
        result = command._run_docker_control(sandbox.docker_path, ["volume", "create", "--label", f"{command.ENVIRONMENT_LABEL}=v1", "--label", f"{command.ENVIRONMENT_SCOPE_LABEL}={scope}", volume])
        if result.returncode != 0 or not _owned_volume(sandbox, volume, scope):
            raise _Unavailable("volume_creation_failed")
        _fresh(workspace, project)
        project = _prepare(sandbox, project, volume, workspace, cancel_event, change_callback)
        _cancel(cancel_event)
        if not _owned_volume(sandbox, volume, scope):
            raise _Unavailable("volume_changed")
        identity = _digest([project.identity, sandbox.image_id, runtime])
        entry = {"volume": volume, "identity": identity, "manifest": project.identity, "image_id": sandbox.image_id, "runtime": runtime}
        _save_record(fd, scope, {"version": 1, "ready": entry, "staging": ready})
        published = True
        cleanup = ()
        if ready and ready["volume"] != volume and not _remove_owned(sandbox, ready["volume"], scope):
            cleanup = (ready["volume"],)
        _save_record(fd, scope, {"version": 1, "ready": entry, "staging": ready if cleanup else None})
        return replace(_environment_result(workspace, project, sandbox, scope, volume, identity), cleanup_required=cleanup)
    except _Unavailable as exc:
        cleaned = _remove_owned(sandbox, volume, scope)
        pending = {"volume": volume, "containers": list(exc.cleanup_required)} if exc.cleanup_required or not cleaned else None
        try:
            _save_record(fd, scope, {"version": 1, "ready": ready, "staging": pending})
        except Exception:
            pass  # Preserve the cleanup warning even when its journal cannot save.
        published = True  # Cleanup has been recorded; do not repeat it in finally.
        cleanup = (*exc.cleanup_required, *((volume,) if not cleaned else ()))
        return EnvironmentResult("unavailable", str(exc), cleanup_required=cleanup)
    except KeyboardInterrupt as exc:
        cleaned = _remove_owned(sandbox, volume, scope)
        containers = getattr(exc, "cleanup_required", ())
        pending = {"volume": volume, "containers": list(containers)} if containers or not cleaned else None
        try:
            _save_record(fd, scope, {"version": 1, "ready": ready, "staging": pending})
        except Exception:
            pass
        published = True
        cleanup = (*getattr(exc, "cleanup_required", ()), *((volume,) if not cleaned else ()))
        if cleanup:
            raise EnvironmentInterrupted(cleanup) from None
        raise
    except (OSError, TypeError, ValueError):
        cleaned = _remove_owned(sandbox, volume, scope)
        try:
            _save_record(fd, scope, {"version": 1, "ready": ready, "staging": None if cleaned else {"volume": volume}})
        except Exception:
            pass
        published = True
        return EnvironmentResult("unavailable", "environment_unavailable", cleanup_required=() if cleaned else (volume,))
    finally:
        if not published:
            cleaned = _remove_owned(sandbox, volume, scope)
            try:
                _save_record(fd, scope, {"version": 1, "ready": ready, "staging": None if cleaned else {"volume": volume}})
            except Exception:
                pass


def _environment_result(workspace, project, sandbox, scope, volume, identity):
    dependency = DependencyMount(volume, scope, project.ecosystem, project.root)
    configured = replace(sandbox, dependency=dependency)
    command._validate_dependency_mount(configured, workspace)
    excluded = (str(Path(project.root) / "node_modules"),) if project.ecosystem == "node" else ()
    return EnvironmentResult("ready", sandbox=configured, identity=identity, excluded_dirs=excluded, manifest_identity=project.identity)
