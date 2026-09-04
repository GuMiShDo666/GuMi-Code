from collections.abc import Callable


MAX_TODOS = 20
MAX_TODO_CONTENT_CHARS = 500

_VALID_STATUSES = ("pending", "in_progress", "completed")

_STATUS_MARKERS = {
    "pending": "[ ]",
    "in_progress": "[>]",
    "completed": "[x]",
}


class TodoManager:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def update(
        self,
        todos: object,
        before_update_callback: Callable[[], None] | None = None,
    ) -> str:
        if not isinstance(todos, list):
            raise ValueError("todos 必须是列表")

        if len(todos) > MAX_TODOS:
            raise ValueError(f"todo 数量不能超过 {MAX_TODOS}")

        normalized: list[dict[str, str]] = []
        in_progress_seen = False

        for index, item in enumerate(todos):
            if not isinstance(item, dict):
                raise ValueError(f"第 {index + 1} 项必须是字典")

            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"第 {index + 1} 项的 content 必须是非空字符串")

            if len(content) > MAX_TODO_CONTENT_CHARS:
                raise ValueError(
                    f"第 {index + 1} 项的 content 不能超过 "
                    f"{MAX_TODO_CONTENT_CHARS} 字符"
                )

            status = item.get("status")
            if status not in _VALID_STATUSES:
                raise ValueError(f"第 {index + 1} 项的 status 非法：{status!r}")

            if status == "in_progress":
                if in_progress_seen:
                    raise ValueError("同时只能有一个 in_progress")
                in_progress_seen = True

            normalized.append(
                {
                    "content": content.strip(),
                    "status": status,
                }
            )

        if before_update_callback is not None:
            before_update_callback()
        self.items = normalized
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "No todos."

        lines = [
            f"{_STATUS_MARKERS[item['status']]} {item['content']}"
            for item in self.items
        ]

        total = len(self.items)
        completed = sum(
            1 for item in self.items if item["status"] == "completed"
        )

        lines.append(f"({completed}/{total} completed)")

        return "\n".join(lines)
