"""Small, dependency-free contracts for capabilities and JSON-shaped data."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class ContractError(ValueError):
    pass


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Enforce the JSON Schema subset used by the local capability contracts."""
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ContractError(f"{path} must be an object")
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise ContractError(f"{path} is missing required field(s): {', '.join(missing)}")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], f"{path}.{key}")
    elif expected == "array":
        if not isinstance(value, list):
            raise ContractError(f"{path} must be an array")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_schema(item, schema["items"], f"{path}[{index}]")
    elif expected == "string":
        if not isinstance(value, str):
            raise ContractError(f"{path} must be a string")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1_000_000):
            raise ContractError(f"{path} has an invalid length")
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError(f"{path} must be an integer")
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise ContractError(f"{path} must be a boolean")
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractError(f"{path} must be a number")
    elif expected == "null":
        if value is not None:
            raise ContractError(f"{path} must be null")
    elif expected is not None:
        raise ContractError(f"Unsupported schema type: {expected}")


@dataclass(frozen=True)
class Capability:
    id: str
    purpose: str
    task_types: tuple[str, ...]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    execute: Callable[[dict[str, Any]], dict[str, Any]]
    division: str
    permitted_tools: tuple[str, ...] = ()
    max_attempts: int = 2
    timeout_ms: int = 30_000
    provider: str = "deterministic-local"
    metadata: dict[str, Any] = field(default_factory=dict)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Capability] = {}

    def register(self, capability: Capability) -> None:
        if capability.id in self._items:
            raise ValueError(f"Capability already registered: {capability.id}")
        if capability.max_attempts < 1 or capability.timeout_ms < 1:
            raise ValueError("Capability execution limits must be positive")
        self._items[capability.id] = capability

    def get(self, capability_id: str) -> Capability:
        try:
            return self._items[capability_id]
        except KeyError as exc:
            raise KeyError(f"Capability is not registered: {capability_id}") from exc

    def select(self, task_type: str, *, permitted_tools: tuple[str, ...] = ()) -> list[Capability]:
        """Return deterministic candidates that support the type and tool boundary."""
        permitted = set(permitted_tools)
        return [item for item in self.all() if task_type in item.task_types and set(item.permitted_tools).issubset(permitted)]

    def all(self) -> list[Capability]:
        return sorted(self._items.values(), key=lambda item: item.id)
