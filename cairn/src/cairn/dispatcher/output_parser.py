from __future__ import annotations

import json
import re
from typing import Any


FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    seen: set[str] = set()

    for candidate in _candidate_segments(text):
        segment = candidate.strip()
        if not segment or segment in seen:
            continue
        seen.add(segment)

        try:
            parsed = json.loads(segment)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed

        for start in _object_start_positions(segment):
            try:
                parsed, _end = decoder.raw_decode(segment[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    raise ValueError("no JSON object found in output")


def _candidate_segments(text: str) -> list[str]:
    segments = [text.strip()]
    segments.extend(match.group(1).strip() for match in FENCED_BLOCK_RE.finditer(text))
    return segments


def _object_start_positions(text: str) -> list[int]:
    return [index for index, char in enumerate(text) if char == "{"]


# A single JSON value larger than this is skipped. The cap is independent of
# the stream length so a multi-gigabyte log cannot be pulled back into one str.
JSON_VALUE_SCAN_CHARS = 2_000_000


def extract_json_object_from_file(path: str, *, max_value_chars: int = JSON_VALUE_SCAN_CHARS) -> dict[str, Any]:
    """Return the first JSON object in ``path`` without reading the file into one string.

    This matches ``extract_json_object`` on the first decodable object: that
    function tries the whole text, then ``raw_decode`` from each ``{``. The
    first success wins, so a forward scan is the same result when an object exists.
    """
    decoder = json.JSONDecoder()
    buf = ""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        while True:
            start = buf.find("{")
            if start < 0:
                chunk = handle.read(65536)
                if not chunk:
                    break
                buf = chunk
                continue
            if start:
                buf = buf[start:]
            try:
                parsed, _end = decoder.raw_decode(buf)
            except json.JSONDecodeError as exc:
                if len(buf) < max_value_chars and _json_needs_more(exc, buf):
                    chunk = handle.read(65536)
                    if not chunk:
                        buf = buf[1:]
                        continue
                    buf += chunk
                    if len(buf) > max_value_chars:
                        # This value does not fit in the scan window. Drop its
                        # opening brace and keep only a bounded tail.
                        buf = buf[1:]
                        if len(buf) > max_value_chars:
                            buf = buf[-max_value_chars:]
                    continue
                buf = buf[1:]
                continue
            if isinstance(parsed, dict):
                return parsed
            buf = buf[1:]
    raise ValueError("no JSON object found in output")


def _json_needs_more(exc: json.JSONDecodeError, buf: str) -> bool:
    if exc.pos >= len(buf):
        return True
    return exc.msg.startswith("Unterminated")
