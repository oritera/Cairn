from __future__ import annotations

from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.dispatcher.workers.base import WorkerDriver


DRIVERS: dict[str, WorkerDriver] = {
    "pi": PiDriver(),
}

# Local Pi invokes the host CLI in its native configuration without Cairn provider injection.
LOCAL_DRIVERS: dict[str, WorkerDriver] = {
    "pi": PiDriver(local=True),
}


def get_driver(name: str, execution: str = "container") -> WorkerDriver:
    drivers = LOCAL_DRIVERS if execution == "local" else DRIVERS
    return drivers[name]
