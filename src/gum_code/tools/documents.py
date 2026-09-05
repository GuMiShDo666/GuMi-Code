"""Private, bounded document snapshots and text-only model-facing tool results."""

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from gum_code.tools.common import _is_sensitive_path, _reject_symlinks, _resolve_workspace_path
from gum_code.tools.document_worker import (
    MAX_DOCUMENT_BYTES, MAX_IMAGE_BYTES, MAX_PDF_BATCH, MAX_DOCX_BATCH,
    MAX_PNG_BYTES, MAX_WORKER_RESULT_BYTES,
)

MAX_QUESTION_CHARS = 4000
MAX_DOCUMENT_RESULT_CHARS = 20_000
WORKER_TIMEOUT_SECONDS = 10
_WORKERS = threading.BoundedSemaphore(2)
_VERSION = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_REF = re.compile(r"([0-9a-f]{64}):([1-9][0-9]{0,5}):(0|[1-9][0-9]{0,5})\Z")
_WORKER = Path(__file__).with_name("document_worker.py")


def _integer(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _path(workspace: Path, path: str) -> tuple[Path, str]:
    if (not isinstance(path, str) or not path or len(path) > 1024 or ":" in path
            or "\\" in path or any(ord(c) < 32 for c in path)
            or ".." in Path(path).parts or _is_sensitive_path(Path(path))):
        raise ValueError("禁止访问无效或敏感文档路径")
    try:
        _reject_symlinks(workspace, path)
        target = _resolve_workspace_path(workspace, path)
        info = target.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("文档必须是普通文件")
    except (OSError, ValueError):
        raise ValueError("无法安全访问文档") from None
    suffix = target.suffix.lower()
    kind = {".pdf": "pdf", ".docx": "docx", ".png": "image", ".jpg": "image",
            ".jpeg": "image", ".webp": "image"}.get(suffix)
    if kind is None:
        raise ValueError("暂不支持此文件格式")
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_DOCUMENT_BYTES
    if info.st_size > limit:
        raise ValueError("文档超过读取大小上限")
    return target, kind


def _source_version(value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not _VERSION.fullmatch(value)):
        raise ValueError("source_version 无效")


def validate_read_document(workspace: Path, path: str, start: int = 1, count: int | None = None,
                           offset: int = 0, visual_offset: int = 0,
                           source_version: str | None = None, **unknown) -> None:
    """Side-effect-free preflight, repeated by the handler before its snapshot."""
    if unknown:
        raise ValueError("文档参数包含未知字段")
    _, kind = _path(workspace, path)
    if kind not in {"pdf", "docx"}:
        raise ValueError("图片请使用 analyze_image")
    maximum = MAX_PDF_BATCH if kind == "pdf" else MAX_DOCX_BATCH
    if (not _integer(start, 1, 100_000) or not _integer(offset, 0, 8 * 1024 * 1024)
            or not _integer(visual_offset, 0, 100_000)
            or (count is not None and not _integer(count, 1, maximum))):
        raise ValueError("文档读取范围无效")
    _source_version(source_version)


def validate_analyze_image(workspace: Path, path: str, question: str, page: int | None = None,
                          image_ref: str | None = None, crop: list[float] | None = None,
                          source_version: str | None = None, **unknown) -> None:
    if unknown:
        raise ValueError("图片参数包含未知字段")
    _, kind = _path(workspace, path)
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        raise ValueError("图片问题必须是 1 到 4000 字符的文字")
    _source_version(source_version)
    if kind == "pdf":
        if not _integer(page, 1, 1000) or image_ref is not None:
            raise ValueError("PDF 图片分析必须指定单页且不能带图片引用")
    elif kind == "docx":
        if page is not None or not isinstance(image_ref, str) or not _IMAGE_REF.fullmatch(image_ref):
            raise ValueError("DOCX 图片分析必须使用 read_document 返回的图片引用")
        if source_version is not None and image_ref.split(":", 1)[0] != source_version:
            raise ValueError("图片引用和来源版本不匹配")
    elif page is not None or image_ref is not None:
        raise ValueError("独立图片不能指定页码或图片引用")
    if crop is not None:
        if (not isinstance(crop, list) or len(crop) != 4
                or any(type(x) not in {int, float} or not math.isfinite(x) or not 0 <= x <= 1 for x in crop)
                or not crop[0] < crop[2] or not crop[1] < crop[3]):
            raise ValueError("图片区域必须是 0 到 1 之间的有效矩形")


def _snapshot(workspace: Path, path: str, kind: str) -> tuple[bytes, str]:
    """Open each component without following links; hash exactly the decoded snapshot."""
    root = workspace.resolve()
    relative = Path(path)
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ValueError("当前平台不支持安全文档打开")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(fd)
        limit = MAX_IMAGE_BYTES if kind == "image" else MAX_DOCUMENT_BYTES
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError("无法安全读取文档")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        current = os.stat(relative.name, dir_fd=directory, follow_symlinks=False)
        _reject_symlinks(workspace, path)
        visible = _resolve_workspace_path(workspace, path).stat()
        identity = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns)
        if (len(data) > limit or identity(before) != identity(after)
                or identity(after) != identity(current) or identity(current) != identity(visible)):
            raise ValueError("文档在读取时发生变化")
        return data, hashlib.sha256(data).hexdigest()
    except OSError:
        raise ValueError("无法安全读取文档") from None
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def _private_read(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()):
            raise ValueError("解析结果无效")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("解析结果超限")
        return data


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "content": "", "unparsed": [], "next_cursor": None}


def _temporary_parent(workspace: Path) -> Path:
    # Do not inherit TMPDIR: it may point into the model-readable workspace.
    root = workspace.resolve()
    for candidate in (Path("/tmp"), Path("/var/tmp")):
        parent = candidate.resolve()
        if parent.is_dir() and not parent.is_relative_to(root):
            return parent
    raise ValueError("无法建立私密解析目录")


def _run_worker(data: bytes, options: dict, cancel_event: threading.Event | None = None,
                *, workspace: Path) -> tuple[dict, bytes | None]:
    while not _WORKERS.acquire(timeout=0.05):
        if cancel_event is not None and cancel_event.is_set():
            return _unavailable("cancelled"), None
    try:
        if cancel_event is not None and cancel_event.is_set():
            return _unavailable("cancelled"), None
        with tempfile.TemporaryDirectory(prefix="gum-code-document-", dir=_temporary_parent(workspace)) as temporary:
            directory = Path(temporary)
            os.chmod(directory, 0o700)
            for name, payload in (("input.bin", data), ("request.json", json.dumps(options).encode("utf-8"))):
                with (directory / name).open("xb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(payload)
            process = subprocess.Popen([sys.executable, "-I", str(_WORKER), temporary],
                                       cwd=temporary, env={"PYTHONIOENCODING": "utf-8"},
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            import time
            deadline = time.monotonic() + WORKER_TIMEOUT_SECONDS
            try:
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        return _unavailable("cancelled"), None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return _unavailable("parse_timeout"), None
                    try:
                        process.wait(timeout=min(0.05, remaining))
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
            if process.returncode != 0:
                return _unavailable("parse_failed"), None
            result = json.loads(_private_read(directory / "result.json", MAX_WORKER_RESULT_BYTES))
            if not isinstance(result, dict) or result.get("status") not in {"ok", "partial", "unavailable"}:
                return _unavailable("parse_failed"), None
            image = None
            if options["operation"] == "analyze" and result["status"] != "unavailable":
                image = _private_read(directory / "image.png", MAX_PNG_BYTES)
                if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                    return _unavailable("parse_failed"), None
            return result, image
    except (OSError, ValueError):
        return _unavailable("parse_failed"), None
    finally:
        _WORKERS.release()


def _result(result: dict, path: str, version: str, **location) -> str:
    result["source"] = {"path": Path(path).as_posix(), "source_version": version, **location}
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > MAX_DOCUMENT_RESULT_CHARS and result.get("content_kind") == "vision_analysis_not_original_text":
        original = result.get("content", "")
        if isinstance(original, str):
            result.update(status="partial", reason="vision_output_limit")
            lo, hi = 0, len(original)
            while lo < hi:
                middle = (lo + hi + 1) // 2
                result["content"] = original[:middle]
                if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= MAX_DOCUMENT_RESULT_CHARS:
                    lo = middle
                else:
                    hi = middle - 1
            result["content"] = original[:lo]
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > MAX_DOCUMENT_RESULT_CHARS:
        return json.dumps({**_unavailable("result_limit"), "source": result["source"]}, ensure_ascii=False)
    return encoded


def read_document(workspace: Path, path: str, start: int = 1, count: int | None = None,
                  offset: int = 0, visual_offset: int = 0, source_version: str | None = None,
                  *, cancel_event: threading.Event | None = None, **unknown) -> str:
    """Read local text and object references only; no implicit vision request."""
    validate_read_document(workspace, path, start, count, offset, visual_offset, source_version, **unknown)
    _, kind = _path(workspace, path)
    data, version = _snapshot(workspace, path, kind)
    if source_version is not None and source_version != version:
        return _result(_unavailable("source_changed"), path, version)
    options = {"operation": "read", "kind": kind, "start": start,
               "count": count if count is not None else (MAX_PDF_BATCH if kind == "pdf" else MAX_DOCX_BATCH),
               "offset": offset, "visual_offset": visual_offset, "source_version": version}
    result, _ = _run_worker(data, options, cancel_event, workspace=workspace)
    return _result(result, path, version, start=start)


def analyze_image(workspace: Path, path: str, question: str, page: int | None = None,
                  image_ref: str | None = None, crop: list[float] | None = None,
                  source_version: str | None = None, *, vision_runtime=None,
                  cancel_event: threading.Event | None = None, **unknown) -> str:
    """Decode one selected image, then ask only the host-authorized shared runtime."""
    validate_analyze_image(workspace, path, question, page, image_ref, crop, source_version, **unknown)
    _, kind = _path(workspace, path)
    data, version = _snapshot(workspace, path, kind)
    expected = source_version or (image_ref.split(":", 1)[0] if image_ref else None)
    location = {key: value for key, value in {"page": page, "image_ref": image_ref, "crop": crop}.items()
                if value is not None}
    if expected is not None and expected != version:
        return _result(_unavailable("source_changed"), path, version, **location)
    options = {"operation": "analyze", "kind": kind, "page": page,
               "image_ref": image_ref, "crop": crop, "source_version": version}
    result, image = _run_worker(data, options, cancel_event, workspace=workspace)
    if image is not None:
        if vision_runtime is None:
            vision = _unavailable("vision_disabled")
        else:
            kwargs = {"cancel_event": cancel_event} if cancel_event is not None else {}
            vision = vision_runtime.analyze(image, question, **kwargs)
        result.update({key: vision[key] for key in ("status", "content", "reason") if key in vision})
        result["content_kind"] = "vision_analysis_not_original_text"
        if result.get("unparsed"):
            result["source_limitation"] = "文档底图或预览，未还原文档显示变换；不能将全部像素视为文档可见正文。"
        if result.get("unparsed") and result.get("status") == "ok":
            result["status"] = "partial"
    return _result(result, path, version, **location)
