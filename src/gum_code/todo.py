import copy
import json
import uuid
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


MAX_PLAN_STATE_BYTES = 1024 * 1024


def validate_plan_state(value: object) -> dict[str, object]:
    """Validate a bounded historical projection; it never carries execution grants."""
    if not isinstance(value, dict) or set(value) != {"version", "current_plan_id", "plans"}:
        raise ValueError("计划状态格式无效")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("计划状态版本无效")
    plans = value["plans"]
    if not isinstance(plans, list):
        raise ValueError("计划列表格式无效")
    ids = set()
    for plan in plans:
        if not isinstance(plan, dict) or set(plan) != {"id", "title", "items"}:
            raise ValueError("计划格式无效")
        plan_id = plan["id"]
        if not isinstance(plan_id, str) or len(plan_id) != 32:
            raise ValueError("计划标识无效")
        try:
            if uuid.UUID(hex=plan_id).hex != plan_id or plan_id in ids:
                raise ValueError("计划标识无效")
        except (ValueError, AttributeError):
            raise ValueError("计划标识无效") from None
        ids.add(plan_id)
        title = plan["title"]
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TODO_CONTENT_CHARS:
            raise ValueError("计划标题无效")
        manager = TodoManager()
        manager.update(plan["items"])
        if manager.items != plan["items"]:
            raise ValueError("计划事项必须规范化")
    current = value["current_plan_id"]
    if (plans and (not isinstance(current, str) or current not in ids)) or (not plans and current is not None):
        raise ValueError("当前计划标识无效")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_PLAN_STATE_BYTES:
        raise ValueError("计划状态超过单次保存上限；已有计划未删除")
    return copy.deepcopy(value)


def validate_plan_transition(previous: object, value: object) -> dict[str, object]:
    state = validate_plan_state(value)
    if previous is not None:
        prior = validate_plan_state(previous)
        current = {plan["id"]: plan for plan in state["plans"]}
        for old in prior["plans"]:
            new = current.get(old["id"])
            if new is None or old["title"] != new["title"]:
                raise ValueError("已有计划不能删除或改名")
            if old["id"] != prior["current_plan_id"] or old["id"] != state["current_plan_id"]:
                if new != old:
                    raise ValueError("暂停计划不能改写")
    return state


class PlanRuntime:
    """One current checklist and retained suspended checklists, scoped by the host Session."""

    def __init__(self, initial_state: object = None) -> None:
        self._state = validate_plan_state(initial_state) if initial_state is not None else {
            "version": 1, "current_plan_id": None, "plans": [],
        }
        self._user_request = ""
        self._switched = False
        self._save: Callable[[dict[str, object]], None] | None = None

    def start_request(self, user_request: str, save: Callable[[dict[str, object]], None] | None = None) -> None:
        self._user_request = user_request
        self._switched = False
        self._save = save

    def export_state(self) -> dict[str, object]:
        return validate_plan_state(self._state)

    def lock_switching(self) -> None:
        """Completion recovery continues the selected task; it cannot replace its obligations."""
        self._switched = True

    @property
    def items(self) -> list[dict[str, str]]:
        return copy.deepcopy(next((plan["items"] for plan in self._state["plans"]
                                   if plan["id"] == self._state["current_plan_id"]), []))

    def _commit(self, state: dict[str, object]) -> None:
        state = validate_plan_transition(self._state, state)
        if self._save is not None:
            try:
                self._save(copy.deepcopy(state))
            except Exception as error:
                raise RuntimeError("计划保存失败（提交未确认）；内存计划未替换，磁盘状态需核对") from error
        self._state = state

    def update(self, todos: object, before_update_callback: Callable[[], None] | None = None) -> str:
        manager = TodoManager()
        result = manager.update(todos)
        state = self.export_state()
        if state["current_plan_id"] is None:
            plan = {"id": uuid.uuid4().hex, "title": self._user_request[:MAX_TODO_CONTENT_CHARS] or "当前任务", "items": []}
            state["plans"].append(plan)
            state["current_plan_id"] = plan["id"]
        plan = next(plan for plan in state["plans"] if plan["id"] == state["current_plan_id"])
        plan["items"] = manager.items
        validate_plan_state(state)
        if before_update_callback is not None:
            before_update_callback()
        self._commit(state)
        return result

    def control(self, arguments: object, before_execute_callback: Callable[[], None] | None = None) -> str:
        if not isinstance(arguments, dict):
            raise ValueError("计划操作必须是对象")
        action = arguments.get("action")
        if action == "list":
            if set(arguments) - {"action", "offset"}:
                raise ValueError("计划列表参数无效")
            offset = arguments.get("offset", 0)
            if type(offset) is not int or offset < 0:
                raise ValueError("计划列表偏移无效")
            plans = self._state["plans"]
            page = [{"id": p["id"], "title": p["title"], "current": p["id"] == self._state["current_plan_id"]}
                    for p in plans[offset:offset + 20]]
            if before_execute_callback is not None:
                before_execute_callback()
            return json.dumps({"plans": page, "next_offset": offset + 20 if offset + 20 < len(plans) else None}, ensure_ascii=False)
        required = {"action", "user_request", "title" if action == "new" else "plan_id"}
        if action not in {"new", "resume"} or set(arguments) != required:
            raise ValueError("计划操作参数无效")
        if not self._user_request or arguments["user_request"] != self._user_request:
            raise ValueError("切换计划必须绑定当前原始用户请求")
        if self._switched:
            raise ValueError("本轮已切换计划；不得反复切换规避完成要求")
        state = self.export_state()
        if action == "new":
            plan = {"id": uuid.uuid4().hex, "title": arguments["title"], "items": []}
            state["plans"].append(plan)
            state["current_plan_id"] = plan["id"]
        else:
            plan_id = arguments["plan_id"]
            if not isinstance(plan_id, str) or not any(p["id"] == plan_id for p in state["plans"]):
                raise ValueError("计划不存在")
            state["current_plan_id"] = plan_id
        validate_plan_state(state)
        if before_execute_callback is not None:
            before_execute_callback()
        self._commit(state)
        self._switched = True
        return self.render()

    def render(self) -> str:
        current = next((p for p in self._state["plans"] if p["id"] == self._state["current_plan_id"]), None)
        return json.dumps({"current_plan": current, "retained_plan_count": len(self._state["plans"])}, ensure_ascii=False)
