from __future__ import annotations

from cairn.dispatcher.workers.adapters import ClaudeCodeDriver, CodexDriver, MockDriver, PiDriver
from cairn.dispatcher.workers.base import WorkerDriver


_CLAUDE = ClaudeCodeDriver()

DRIVERS: dict[str, WorkerDriver] = {
    "claudecode": _CLAUDE,
    "codex": CodexDriver(),
    "pi": PiDriver(),
    "mock": MockDriver(),
}

# Local variants invoke the host CLIs in their native configuration (no cairn provider
# injection). claudecode builds identical commands in both modes, so it is shared; mock
# has a local variant that runs on the dispatcher's own interpreter (portable to Windows).
LOCAL_DRIVERS: dict[str, WorkerDriver] = {
    "claudecode": _CLAUDE,
    "codex": CodexDriver(local=True),
    "pi": PiDriver(local=True),
    "mock": MockDriver(local=True),
}


def get_driver(name: str, execution: str = "container") -> WorkerDriver:
    drivers = LOCAL_DRIVERS if execution == "local" else DRIVERS
    return drivers[name]
