"""Typed tool registry. The model can only call tools registered here, with validated arguments."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from ..policy import Risk


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class Tool:
    name: str
    description: str
    params: type[BaseModel]
    run: Callable[..., Any]  # (args, ctx) -> dict|str  (sync or async)
    risk: Risk | Callable[[Any], Risk] = Risk.READ
    timeout: float = 60.0
    control: bool = False  # drives mouse/keyboard -> needs a control-session approval
    untrusted: bool = False  # output is external content -> wrapped as data
    preview: Callable[..., Any] | None = None  # async (args, ctx) -> {"text": str, "digest": str|None}
    check: Callable[..., Any] | None = None  # (args, ctx) raises PolicyError before any approval prompt
    reversible: bool = True

    def risk_for(self, args: BaseModel) -> Risk:
        return self.risk(args) if callable(self.risk) else self.risk

    def spec(self) -> dict:
        schema = self.params.model_json_schema()
        schema.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": schema}}


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict]:
        return [t.spec() for t in self._tools.values()]
