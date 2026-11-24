from __future__ import annotations

from cairn.dispatcher.workers.adapters import ClaudeCodeDriver, CodexDriver, GrokDriver, MockDriver, PiDriver
from cairn.dispatcher.workers.base import WorkerDriver


_CLAUDE = ClaudeCodeDriver()
_MOCK = MockDriver()

DRIVERS: dict[str, WorkerDriver] = {
    "claudecode": _CLAUDE,
    "codex": CodexDriver(),
    "grok": GrokDriver(),
    "pi": PiDriver(),
    "mock": _MOCK,
}

# Local variants invoke the host CLIs in their native configuration (no cairn provider
# injection). claudecode and mock build identical commands in both modes, so they are shared.
LOCAL_DRIVERS: dict[str, WorkerDriver] = {
    "claudecode": _CLAUDE,
    "codex": CodexDriver(local=True),
    "grok": GrokDriver(local=True),
    "pi": PiDriver(local=True),
    "mock": _MOCK,
}


def get_driver(name: str, execution: str = "container") -> WorkerDriver:
    drivers = LOCAL_DRIVERS if execution == "local" else DRIVERS
    return drivers[name]
