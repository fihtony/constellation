"""TodoManager for connect-agent task planning."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TodoItem:
    content: str
    status: str = "pending"


class TodoManager:
    MAX_ITEMS = 20

    def __init__(self) -> None:
        self._items: list[TodoItem] = []
        self._turns_since_update = 0

    @property
    def items(self) -> list[TodoItem]:
        return list(self._items)

    def update(self, items: list[dict]) -> str:
        self._turns_since_update = 0
        self._items = [
            TodoItem(
                content=entry.get("content", ""),
                status=entry.get("status", "pending"),
            )
            for entry in items[: self.MAX_ITEMS]
        ]
        return self.render()

    def tick(self) -> str | None:
        self._turns_since_update += 1
        if self._turns_since_update >= 3 and any(item.status != "completed" for item in self._items):
            return (
                "<reminder>You have not updated your todo list for "
                f"{self._turns_since_update} turns. "
                "Please review and update your plan.</reminder>"
            )
        return None

    def render(self) -> str:
        if not self._items:
            return "Todo list is empty."
        lines: list[str] = []
        for idx, item in enumerate(self._items, 1):
            marker = {"pending": "○", "in_progress": "●", "completed": "✓"}.get(item.status, "○")
            lines.append(f"  {marker} {idx}. [{item.status}] {item.content}")
        return "Todo list:\n" + "\n".join(lines)