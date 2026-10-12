"""Bounded root AGENTS.md loading; rules never grant tool permissions."""
import os
import stat
from pathlib import Path

START = "<gum-code-project-rules>"
END = "</gum-code-project-rules>"
MAX_BYTES = 64 * 1024

class ProjectRulesError(ValueError):
    pass

def apply_project_rules(content: str, workspace: Path):
    start, end = content.find(START), content.find(END)
    if start >= 0 and end >= start:
        content = (content[:start].rstrip() + content[end + len(END):]).strip()
    target = workspace / "AGENTS.md"
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return content, False
    except OSError:
        raise ProjectRulesError("AGENTS.md 无法读取，请检查文件权限；不支持符号链接。") from None
    try:
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ProjectRulesError("AGENTS.md 必须是普通文本文件。")
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ProjectRulesError("AGENTS.md 超过 64 KiB，请精简后重试。")
        rules = raw.decode("utf-8-sig").strip()
    except UnicodeError:
        raise ProjectRulesError("AGENTS.md 需要使用 UTF-8 编码，请转换后重试。") from None
    except OSError:
        raise ProjectRulesError("AGENTS.md 读取失败，请检查文件后重试。") from None
    if not rules:
        return content, False
    rules = rules.replace(START, "&lt;gum-code-project-rules&gt;").replace(END, "&lt;/gum-code-project-rules&gt;")
    return content + "\n\n" + START + "\n以下是本轮重新读取的当前工作目录根部 AGENTS.md，是项目约定的当前版本。历史回复中的旧约定不再生效，不要模仿旧答复代替遵守当前规则。遵守适用规则；用户当前明确要求优先。规则不授予文件、命令、联网或审批权限，不得绕过宿主限制。\n\n" + rules + "\n" + END, True
