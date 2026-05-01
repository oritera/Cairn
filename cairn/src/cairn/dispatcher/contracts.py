from __future__ import annotations

from typing import Any

from cairn.dispatcher.output_parser import extract_json_object


def parse_json_output(stdout: str) -> dict[str, Any]:
    return extract_json_object(stdout)


def validate_reason_payload(
    payload: dict[str, Any], open_intents_empty: bool, max_intents: int,
) -> tuple[str, dict[str, Any] | list[dict[str, Any]] | None]:
    if payload.get("accepted") is False:
        return "rejected", None
    if payload.get("accepted") is not True:
        raise ValueError("accepted must be true or false")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    complete = data.get("complete")
    intents = data.get("intents")
    # backward compat: accept singular "intent" key from LLMs
    if intents is None:
        singular = data.get("intent")
        if isinstance(singular, dict):
            intents = [singular]
    if complete is not None:
        if intents is not None:
            raise ValueError("complete and intents cannot coexist")
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise ValueError("invalid complete payload")
        return "complete", complete
    if intents is not None:
        if not isinstance(intents, list):
            raise ValueError("intents must be an array")
        for i, intent in enumerate(intents):
            if not isinstance(intent, dict) or "from" not in intent or "description" not in intent:
                raise ValueError(f"invalid intent at index {i}")
        if not intents and open_intents_empty:
            raise ValueError("intents must not be empty when open_intents is empty")
        intents = intents[:max_intents]
        if not intents:
            return "noop", None
        return "intents", intents
    if open_intents_empty:
        raise ValueError("intents is required when open_intents is empty")
    return "noop", None


def validate_bootstrap_execute_payload(payload: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
    if payload.get("accepted") is False:
        return "rejected", None
    if payload.get("accepted") is not True:
        raise ValueError("accepted must be true or false")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("data must be an object")

    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")

    result = {"fact_description": fact_description.strip()}
    complete = data.get("complete")
    if complete is None:
        raise ValueError("complete is required")
    if not isinstance(complete, dict):
        raise ValueError("complete must be an object")
    complete_description = complete.get("description")
    if not isinstance(complete_description, str) or not complete_description.strip():
        raise ValueError("complete.description is required")
    result["complete_description"] = complete_description.strip()
    return "complete", result


def validate_bootstrap_conclude_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    if payload.get("accepted") is False:
        return "rejected", None
    if payload.get("accepted") is not True:
        raise ValueError("accepted must be true or false")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("data must be an object")

    if data.get("complete") is not None:
        raise ValueError("complete is not allowed")
    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")
    return "fact", fact_description.strip()


def validate_explore_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    if payload.get("accepted") is False:
        return "rejected", None
    if payload.get("accepted") is not True:
        raise ValueError("accepted must be true or false")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    return "fact", description.strip()
