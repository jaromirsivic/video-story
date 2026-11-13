"""Structured failures shared by launchers, CLI and MCP."""

from typing import Any


class HostError(RuntimeError):
    def __init__(self, code: str, message: str, exit_code: int = 1, **details: Any):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "message": str(self), **self.details}
