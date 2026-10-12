"""Read Codex user Skills as bounded, non-executable Agent context."""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_ACTIVE_SKILLS = 3
MAX_SKILL_BYTES = 256 * 1024
MAX_RESOURCE_BYTES = 1024 * 1024
MAX_RESOURCE_CHARS = 20_000
MAX_RESOURCE_PATHS = 100
SKILL_SECTION_START = "<gum-code-skills>"
SKILL_SECTION_END = "</gum-code-skills>"
SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# Slash mentions start a whitespace-delimited token; do not match a path prefix.
# Keep dollar mentions readable in existing conversations.
_MENTION = re.compile(
    r"(?<!\S)/([A-Za-z0-9][A-Za-z0-9._-]{0,127})(?![\w./-])"
    r"|(?<![\w.-])\$([A-Za-z0-9][A-Za-z0-9._-]{0,127})(?![\w.-])"
)


SKILL_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "find_skills",
            "description": "Find available user Skills by name or description.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 200}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Load one Skill's instructions for this task only.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "maxLength": 128}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill_resource",
            "description": "Read bounded UTF-8 text from an already loaded Skill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "maxLength": 128},
                    "path": {"type": "string", "maxLength": 500},
                    "offset": {"type": "integer", "minimum": 0},
                    "length": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_RESOURCE_CHARS,
                    },
                },
                "required": ["name", "path"],
                "additionalProperties": False,
            },
        },
    },
]
SKILL_TOOL_NAMES = frozenset(
    schema["function"]["name"] for schema in SKILL_TOOL_SCHEMAS
)


@dataclass(frozen=True)
class SkillInfo:
    name: str
    display_name: str
    description: str
    directory: Path


@dataclass(frozen=True)
class SkillSnapshot:
    name: str
    display_name: str
    description: str
    directory: Path
    content: str
    sha256: str
    resources: tuple[str, ...]
    source: str


def default_skill_root() -> Path:
    configured = os.getenv("CODEX_HOME")
    codex_home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return codex_home / "skills"


def _frontmatter(text: str) -> tuple[str | None, str | None]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return None, None
    values: dict[str, str] = {}
    index = 1
    while index < end:
        line = lines[index]
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"name", "description"}:
            value = value.strip()
            if value in {"|", ">", "|-", ">-"}:
                parts: list[str] = []
                index += 1
                while index < end and (not lines[index].strip() or lines[index][:1].isspace()):
                    parts.append(lines[index].strip())
                    index += 1
                values[key.strip()] = (
                    " ".join(part for part in parts if part)
                    if value.startswith(">")
                    else "\n".join(parts)
                )
                continue
            values[key.strip()] = value.strip("\"'")
        index += 1
    return values.get("name"), values.get("description")


def _read_skill_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Skill 缺少安全的 SKILL.md")
    if path.stat().st_size > MAX_SKILL_BYTES:
        raise ValueError(f"Skill SKILL.md 超过 {MAX_SKILL_BYTES} 字节上限")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Skill SKILL.md 不是有效 UTF-8") from error


def _skill_info(directory: Path, *, strict: bool) -> SkillInfo | None:
    if directory.is_symlink() or not directory.is_dir() or SKILL_NAME.fullmatch(directory.name) is None:
        if strict:
            raise ValueError("Skill 目录无效")
        return None
    try:
        text = _read_skill_file(directory / "SKILL.md")
    except (OSError, ValueError):
        if strict:
            raise ValueError(f"Skill 不可用：{directory.name}") from None
        return None
    frontmatter_name, description = _frontmatter(text)
    if not description:
        if strict:
            raise ValueError(f"Skill frontmatter 无效：{directory.name}")
        return None
    return SkillInfo(
        name=directory.name,
        display_name=frontmatter_name or directory.name,
        description=description,
        directory=directory,
    )


def discover_skills(root: Path | None = None) -> list[SkillInfo]:
    skill_root = default_skill_root() if root is None else Path(root)
    if not skill_root.exists():
        return []
    if skill_root.is_symlink() or not skill_root.is_dir():
        raise ValueError("Codex Skill 根目录配置无效")
    result = []
    for directory in sorted(skill_root.iterdir(), key=lambda path: path.name.lower()):
        info = _skill_info(directory, strict=False)
        if info is not None:
            result.append(info)
    return result


def _resource_paths(directory: Path) -> tuple[str, ...]:
    resources: list[str] = []
    for path in sorted(directory.rglob("*")):
        if len(resources) >= MAX_RESOURCE_PATHS:
            break
        if path.name == "SKILL.md" or path.is_symlink() or not path.is_file():
            continue
        resources.append(path.relative_to(directory).as_posix())
    return tuple(resources)


def _load_snapshot(info: SkillInfo, source: str) -> SkillSnapshot:
    content = _read_skill_file(info.directory / "SKILL.md")
    return SkillSnapshot(
        name=info.name,
        display_name=info.display_name,
        description=info.description,
        directory=info.directory,
        content=content,
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        resources=_resource_paths(info.directory),
        source=source,
    )


class SkillRuntime:
    """Own the active Skill set for exactly one root-Agent task."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        sticky_names: tuple[str, ...] | list[str] = (),
        activation_callback: Callable[[SkillSnapshot], None] | None = None,
    ) -> None:
        self.root = default_skill_root() if root is None else Path(root)
        self.sticky_names = tuple(dict.fromkeys(sticky_names))
        if len(self.sticky_names) > MAX_ACTIVE_SKILLS:
            raise ValueError("Session sticky Skills 不能超过 3 个")
        self.activation_callback = activation_callback
        self.active: dict[str, SkillSnapshot] = {}

    def _find_exact(self, name: str) -> SkillInfo:
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 128
            or Path(name).name != name
            or any(ord(character) < 32 for character in name)
        ):
            raise ValueError("Skill 名称无效")
        skill_root = self.root
        if not skill_root.exists():
            raise ValueError(f"Skill 不可用：{name}")
        if skill_root.is_symlink() or not skill_root.is_dir():
            raise ValueError("Codex Skill 根目录配置无效")
        candidate = skill_root / name
        try:
            if candidate.parent.resolve() != skill_root.resolve():
                raise ValueError("Skill 名称越界")
        except OSError as error:
            raise ValueError(f"Skill 不可用：{name}") from error
        info = _skill_info(candidate, strict=True)
        assert info is not None
        return info

    def _activate(self, name: str, source: str) -> SkillSnapshot:
        existing = self.active.get(name)
        if existing is not None:
            return existing
        if len(self.active) >= MAX_ACTIVE_SKILLS:
            raise ValueError("当前任务最多激活 3 个 Skills")
        snapshot = _load_snapshot(self._find_exact(name), source)
        self.active[name] = snapshot
        try:
            if self.activation_callback is not None:
                self.activation_callback(snapshot)
        except BaseException:
            self.active.pop(name, None)
            raise
        return snapshot

    def start_task(self, user_query: str) -> None:
        explicit = tuple(dict.fromkeys(
            match.group(1) or match.group(2) for match in _MENTION.finditer(user_query)
        ))
        sticky = tuple(dict.fromkeys((*self.sticky_names, *explicit)))
        if len(sticky) > MAX_ACTIVE_SKILLS:
            raise ValueError("Session 显式 Skills 不能超过 3 个")
        snapshots = [_load_snapshot(self._find_exact(name), "explicit") for name in sticky]
        self.active.clear()
        self.sticky_names = sticky
        for snapshot in snapshots:
            self.active[snapshot.name] = snapshot
            if self.activation_callback is not None:
                self.activation_callback(snapshot)

    def apply_to_system(self, content: str) -> str:
        start = content.find(SKILL_SECTION_START)
        end = content.find(SKILL_SECTION_END)
        if start >= 0 and end >= start:
            content = (
                content[:start].rstrip()
                + content[end + len(SKILL_SECTION_END) :]
            ).strip()
        if not self.active:
            return content
        sections = [SKILL_SECTION_START]
        for snapshot in self.active.values():
            sections.append(
                f"\n[Skill: {snapshot.name}; sha256={snapshot.sha256}]\n"
                + snapshot.content
            )
        sections.append(SKILL_SECTION_END)
        return content.rstrip() + "\n\n" + "\n".join(sections)

    def find(self, query: str) -> str:
        if not isinstance(query, str):
            return "Error: query 必须是字符串"
        needle = query.casefold().strip()
        matches = [
            {
                "name": info.name,
                "display_name": info.display_name,
                "description": info.description,
            }
            for info in discover_skills(self.root)
            if not needle
            or needle in info.name.casefold()
            or needle in info.display_name.casefold()
            or needle in info.description.casefold()
        ][:20]
        return json.dumps({"skills": matches}, ensure_ascii=False, separators=(",", ":"))

    def load(self, name: str) -> str:
        try:
            snapshot = self._activate(name, "automatic")
        except ValueError as error:
            return f"Error: {error}"
        return json.dumps(
            {
                "name": snapshot.name,
                "display_name": snapshot.display_name,
                "description": snapshot.description,
                "sha256": snapshot.sha256,
                "instructions": snapshot.content,
                "resources": list(snapshot.resources),
                "script_execution": "unsupported",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def read_resource(
        self,
        name: str,
        resource: str,
        *,
        offset: int = 0,
        length: int = MAX_RESOURCE_CHARS,
    ) -> str:
        if not isinstance(name, str):
            return "Error: Skill resource 参数无效"
        snapshot = self.active.get(name)
        if snapshot is None:
            return "Error: 必须先 load_skill"
        if (
            not isinstance(resource, str)
            or not resource
            or Path(resource).is_absolute()
            or ".." in Path(resource).parts
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or isinstance(length, bool)
            or not isinstance(length, int)
            or length < 1
            or length > MAX_RESOURCE_CHARS
        ):
            return "Error: Skill resource 参数无效"
        path = snapshot.directory / resource
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError
            resolved = path.resolve()
            root = snapshot.directory.resolve()
            if resolved.parent != root and root not in resolved.parents:
                raise ValueError
            if path.stat().st_size > MAX_RESOURCE_BYTES:
                return f"Error: Skill resource 超过 {MAX_RESOURCE_BYTES} 字节上限"
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return "Error: Skill resource 不是有效 UTF-8"
        except (OSError, ValueError):
            return "Error: Skill resource 不可用或路径越界"
        page = text[offset : offset + length]
        return json.dumps(
            {"content": page, "offset": offset, "has_more": offset + len(page) < len(text)},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def execute(
        self,
        tool_name: str,
        arguments_json: object,
        before_execute_callback: Callable[[], None] | None = None,
    ) -> str:
        if not isinstance(arguments_json, str):
            return "Error: 无法解析参数 JSON"
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError:
            return "Error: 无法解析参数 JSON"
        if not isinstance(arguments, dict):
            return "Error: 参数必须是 JSON 对象"
        if (
            tool_name == "find_skills"
            and set(arguments) == {"query"}
            and isinstance(arguments["query"], str)
        ):
            if before_execute_callback is not None:
                before_execute_callback()
            return self.find(arguments["query"])
        if (
            tool_name == "load_skill"
            and set(arguments) == {"name"}
            and isinstance(arguments["name"], str)
        ):
            if before_execute_callback is not None:
                before_execute_callback()
            return self.load(arguments["name"])
        if (
            tool_name == "read_skill_resource"
            and set(arguments) <= {"name", "path", "offset", "length"}
            and {"name", "path"} <= set(arguments)
            and isinstance(arguments["name"], str)
            and isinstance(arguments["path"], str)
            and isinstance(arguments.get("offset", 0), int)
            and not isinstance(arguments.get("offset", 0), bool)
            and arguments.get("offset", 0) >= 0
            and isinstance(arguments.get("length", MAX_RESOURCE_CHARS), int)
            and not isinstance(arguments.get("length", MAX_RESOURCE_CHARS), bool)
            and 1 <= arguments.get("length", MAX_RESOURCE_CHARS) <= MAX_RESOURCE_CHARS
        ):
            if before_execute_callback is not None:
                before_execute_callback()
            return self.read_resource(
                arguments["name"],
                arguments["path"],
                offset=arguments.get("offset", 0),
                length=arguments.get("length", MAX_RESOURCE_CHARS),
            )
        return "Error: Skill tool 参数无效"
