from __future__ import annotations

import random

from cairn.dispatcher.config import WorkerConfig


def choose_worker(candidates: list[WorkerConfig], running_counts: dict[str, int]) -> list[WorkerConfig]:
    grouped = sorted(
        candidates,
        key=lambda worker: (
            worker.priority,
            running_counts.get(worker.name, 0),
            random.random(),
        ),
    )
    return grouped
