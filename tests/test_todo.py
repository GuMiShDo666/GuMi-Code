import pytest

from vibe_harness.todo import MAX_TODO_CONTENT_CHARS, MAX_TODOS, TodoManager


def test_update_and_render_three_statuses() -> None:
    manager = TodoManager()

    result = manager.update(
        [
            {"content": "  task one  ", "status": "pending"},
            {"content": "task two", "status": "in_progress"},
            {"content": "task three", "status": "completed"},
        ]
    )

    assert manager.items == [
        {"content": "task one", "status": "pending"},
        {"content": "task two", "status": "in_progress"},
        {"content": "task three", "status": "completed"},
    ]

    assert result == (
        "[ ] task one\n"
        "[>] task two\n"
        "[x] task three\n"
        "(1/3 completed)"
    )


def test_render_empty_list() -> None:
    manager = TodoManager()

    result = manager.update([])

    assert manager.items == []
    assert result == "No todos."
    assert manager.render() == "No todos."


def test_update_rejects_more_than_max_todos() -> None:
    manager = TodoManager()
    todos = [
        {"content": f"task {i}", "status": "pending"}
        for i in range(MAX_TODOS + 1)
    ]

    with pytest.raises(ValueError):
        manager.update(todos)

    assert manager.items == []


def test_update_rejects_non_list() -> None:
    manager = TodoManager()

    with pytest.raises(ValueError):
        manager.update("not a list")

    assert manager.items == []


def test_update_rejects_non_dict_item() -> None:
    manager = TodoManager()

    with pytest.raises(ValueError):
        manager.update(
            [
                {"content": "valid", "status": "pending"},
                "not a dict",
            ]
        )

    assert manager.items == []


def test_update_rejects_empty_content() -> None:
    manager = TodoManager()

    with pytest.raises(ValueError):
        manager.update([{"content": "   ", "status": "pending"}])

    assert manager.items == []


def test_update_rejects_missing_or_non_string_content() -> None:
    manager = TodoManager()

    with pytest.raises(ValueError):
        manager.update([{"status": "pending"}])

    with pytest.raises(ValueError):
        manager.update([{"content": 5, "status": "pending"}])

    assert manager.items == []


def test_update_rejects_invalid_status() -> None:
    manager = TodoManager()

    with pytest.raises(ValueError):
        manager.update([{"content": "task", "status": "done"}])

    assert manager.items == []


def test_update_rejects_multiple_in_progress() -> None:
    manager = TodoManager()

    with pytest.raises(ValueError):
        manager.update(
            [
                {"content": "first", "status": "in_progress"},
                {"content": "second", "status": "in_progress"},
            ]
        )

    assert manager.items == []


def test_failed_update_keeps_old_state() -> None:
    manager = TodoManager()
    manager.update([{"content": "keep me", "status": "pending"}])

    with pytest.raises(ValueError):
        manager.update(
            [
                {"content": "ok", "status": "pending"},
                {"content": "bad", "status": "invalid"},
            ]
        )

    assert manager.items == [{"content": "keep me", "status": "pending"}]
    assert manager.render() == "[ ] keep me\n(0/1 completed)"


def test_update_allows_content_at_max_length() -> None:
    manager = TodoManager()
    content = "x" * MAX_TODO_CONTENT_CHARS

    manager.update([{"content": content, "status": "pending"}])

    assert manager.items == [{"content": content, "status": "pending"}]


def test_update_rejects_content_over_max_length() -> None:
    manager = TodoManager()
    content = "x" * (MAX_TODO_CONTENT_CHARS + 1)

    with pytest.raises(ValueError):
        manager.update([{"content": content, "status": "pending"}])

    assert manager.items == []


def test_overlong_content_keeps_old_state_even_with_valid_prefix(
) -> None:
    manager = TodoManager()
    manager.update([{"content": "keep me", "status": "pending"}])

    with pytest.raises(ValueError):
        manager.update(
            [
                {"content": "valid", "status": "pending"},
                {"content": "x" * (MAX_TODO_CONTENT_CHARS + 1), "status": "pending"},
            ]
        )

    assert manager.items == [{"content": "keep me", "status": "pending"}]
    assert manager.render() == "[ ] keep me\n(0/1 completed)"


def test_length_checked_on_raw_string_before_strip() -> None:
    manager = TodoManager()
    padded = "  " + "y" * MAX_TODO_CONTENT_CHARS + "  "

    with pytest.raises(ValueError):
        manager.update([{"content": padded, "status": "pending"}])

    assert manager.items == []


def test_instances_do_not_share_state() -> None:
    first = TodoManager()
    second = TodoManager()

    first.update([{"content": "first task", "status": "pending"}])

    assert second.items == []
    assert second.render() == "No todos."


def test_render_completed_count() -> None:
    manager = TodoManager()
    manager.update(
        [
            {"content": "a", "status": "completed"},
            {"content": "b", "status": "completed"},
            {"content": "c", "status": "pending"},
        ]
    )

    assert manager.render().endswith("(2/3 completed)")
