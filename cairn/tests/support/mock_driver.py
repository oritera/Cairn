from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import random
import sys
from typing import Any

from _pytest.monkeypatch import MonkeyPatch

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers import registry as worker_registry
from cairn.dispatcher.workers.base import DriverResult, SeedSessionDriver
from cairn.dispatcher.workers.health import HealthResult


ALLOWED_OUTCOMES: dict[str, frozenset[str]] = {
    "healthcheck": frozenset({"ok", "fail"}),
    "reason": frozenset({"complete", "intent", "noop", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "explore_execute": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "explore_conclude": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "bootstrap": frozenset({"complete", "fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "bootstrap_conclude": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
}

DEFAULT_BEHAVIOR: dict[str, dict[str, Any]] = {
    "healthcheck": {"delay": [0.05, 0.15], "outcomes": {"ok": "1.0", "fail": "0.0"}},
    "reason": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "complete": "0.0", "intent": "1.0", "noop": "0.0", "rejected": "0.0",
            "invalid_json": "0.0", "invalid_payload": "0.0", "command_fail": "0.0",
        },
    },
    "explore_execute": {
        "delay": [0.05, 0.3],
        "outcomes": {"fact": "1.0", "rejected": "0.0", "invalid_json": "0.0", "invalid_payload": "0.0", "command_fail": "0.0"},
    },
    "explore_conclude": {
        "delay": [0.05, 0.3],
        "outcomes": {"fact": "1.0", "rejected": "0.0", "invalid_json": "0.0", "invalid_payload": "0.0", "command_fail": "0.0"},
    },
    "bootstrap": {
        "delay": [0.05, 0.3],
        "outcomes": {"complete": "1.0", "fact": "0.0", "rejected": "0.0", "invalid_json": "0.0", "invalid_payload": "0.0", "command_fail": "0.0"},
    },
    "bootstrap_conclude": {
        "delay": [0.05, 0.3],
        "outcomes": {"fact": "1.0", "rejected": "0.0", "invalid_json": "0.0", "invalid_payload": "0.0", "command_fail": "0.0"},
    },
}


def resolve_mock_behavior(worker_name: str, env: dict[str, str]) -> dict[str, dict[str, Any]]:
    behavior: dict[str, dict[str, Any]] = {}
    for phase, allowed_outcomes in ALLOWED_OUTCOMES.items():
        key = f"MOCK_{phase.upper()}"
        payload = _parse_phase(worker_name, key, env.get(key), DEFAULT_BEHAVIOR[phase])
        delay = payload.get("delay")
        if not isinstance(delay, list) or len(delay) != 2:
            raise ValueError(f"worker {worker_name} {key}.delay must be a two-element number array")
        min_delay = _non_negative_number(worker_name, f"{key}.delay[0]", delay[0])
        max_delay = _non_negative_number(worker_name, f"{key}.delay[1]", delay[1])
        if max_delay < min_delay:
            raise ValueError(f"worker {worker_name} {key}.delay[1] must be greater than or equal to delay[0]")

        raw_outcomes = payload.get("outcomes")
        if not isinstance(raw_outcomes, dict):
            raise ValueError(f"worker {worker_name} {key}.outcomes must be an object")
        unknown = sorted(set(raw_outcomes) - allowed_outcomes)
        if unknown:
            raise ValueError(f"worker {worker_name} {key}.outcomes has unsupported keys: {', '.join(unknown)}")
        outcomes: dict[str, float] = {}
        total = Decimal("0")
        for outcome in sorted(allowed_outcomes):
            raw = raw_outcomes.get(outcome, DEFAULT_BEHAVIOR[phase]["outcomes"][outcome])
            try:
                value = Decimal(str(raw))
            except InvalidOperation as exc:
                raise ValueError(f"worker {worker_name} {key}.outcomes.{outcome} must be a decimal probability") from exc
            if value < 0 or value > 1:
                raise ValueError(f"worker {worker_name} {key}.outcomes.{outcome} must be between 0 and 1")
            outcomes[outcome] = float(value)
            total += value
        if total != Decimal("1"):
            raise ValueError(f"worker {worker_name} {key}.outcomes probabilities must sum to 1.0, got {total}")

        entry: dict[str, Any] = {"delay": {"min": min_delay, "max": max_delay}, "outcomes": outcomes}
        rules = payload.get("rules")
        if rules is not None:
            if not isinstance(rules, list):
                raise ValueError(f"worker {worker_name} {key}.rules must be an array")
            entry["rules"] = rules
        behavior[phase] = entry
    return behavior


def _parse_phase(worker_name: str, key: str, raw: str | None, default: dict[str, Any]) -> dict[str, Any]:
    if raw is None:
        return json.loads(json.dumps(default))
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"worker {worker_name} {key} must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError(f"worker {worker_name} {key} must be a JSON object")
    return value


def _non_negative_number(worker_name: str, key: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"worker {worker_name} {key} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"worker {worker_name} {key} must be a number") from exc
    if parsed < 0:
        raise ValueError(f"worker {worker_name} {key} must be non-negative")
    return parsed


_SCRIPT = r'''
import json,random,sys,time

try:
    cfg=json.loads(sys.argv[1]); prompt=json.loads(sys.argv[2]); phase=prompt["phase"]; phase_cfg=cfg[phase]
except Exception as exc:
    print(f"mock setup failed: {exc}", file=sys.stderr); raise SystemExit(1)
delay=phase_cfg["delay"]
time.sleep(random.uniform(delay["min"],delay["max"]))
weights=dict(phase_cfg["outcomes"])
if phase=="reason":
    if not prompt.get("open_intents"): weights.pop("noop",None)
    if not prompt.get("fact_ids"): weights.pop("complete",None); weights.pop("intent",None)
choices=[(name,weight) for name,weight in weights.items() if weight>0]
if not choices:
    print(f"mock {phase} has no legal outcomes for prompt context", file=sys.stderr); raise SystemExit(2)

def _rule_matches(rule, prompt):
    fact_ids=prompt.get("fact_ids") or []; open_intents=prompt.get("open_intents") or []
    if "fact_ids_gte" in rule and len(fact_ids)<rule["fact_ids_gte"]: return False
    if "fact_ids_lte" in rule and len(fact_ids)>rule["fact_ids_lte"]: return False
    if "open_intents_empty" in rule and (len(open_intents)==0)!=rule["open_intents_empty"]: return False
    return True

forced=next((rule["force"] for rule in (phase_cfg.get("rules") or []) if _rule_matches(rule,prompt)),None)
if forced is not None: outcome=forced
else:
    pick=random.uniform(0,sum(weight for _,weight in choices)); total=0; outcome=choices[-1][0]
    for name,weight in choices:
        total+=weight
        if pick<=total: outcome=name; break
if phase=="healthcheck": raise SystemExit(0 if outcome=="ok" else 1)
if outcome=="command_fail": print(f"mock {phase} command failed",file=sys.stderr); raise SystemExit(1)
if outcome=="invalid_json": print("{invalid json"); raise SystemExit(0)
if phase=="reason":
    fact_ids=prompt.get("fact_ids") or []; max_i=prompt.get("max_intents",3); from_ids=[random.choice(fact_ids)] if fact_ids else []
    if outcome=="complete": print(json.dumps({"accepted":True,"data":{"complete":{"from":from_ids,"description":f"mock complete from {from_ids[0]}"}}},ensure_ascii=False))
    elif outcome=="intent":
        intents=[]
        for idx in range(random.randint(1,max(1,max_i))):
            fi=[random.choice(fact_ids)] if fact_ids else []; intents.append({"from":fi,"description":f"mock intent {idx+1} from {fi[0] if fi else 'none'}"})
        print(json.dumps({"accepted":True,"data":{"intents":intents}},ensure_ascii=False))
    elif outcome=="noop": print(json.dumps({"accepted":True,"data":{}},ensure_ascii=False))
    elif outcome=="rejected": print(json.dumps({"accepted":False,"reason":"mock_rejected"},ensure_ascii=False))
    else: print(json.dumps({"accepted":True,"data":{"complete":{"description":"mock invalid payload"}}},ensure_ascii=False))
    raise SystemExit(0)
if phase=="bootstrap":
    if outcome=="complete": print(json.dumps({"accepted":True,"data":{"fact":{"description":"mock fact for bootstrap"},"complete":{"description":"mock bootstrap complete from fact"}}},ensure_ascii=False))
    elif outcome=="fact": print(json.dumps({"accepted":True,"data":{"fact":{"description":"mock fact-only bootstrap result"}}},ensure_ascii=False))
    elif outcome=="rejected": print(json.dumps({"accepted":False,"reason":"mock_rejected"},ensure_ascii=False))
    else: print(json.dumps({"accepted":True,"data":{"fact":{"description":"mock invalid payload"}}},ensure_ascii=False))
    raise SystemExit(0)
if phase=="bootstrap_conclude":
    if outcome=="fact": print(json.dumps({"accepted":True,"data":{"fact":{"description":"mock fact for bootstrap_conclude"}}},ensure_ascii=False))
    elif outcome=="rejected": print(json.dumps({"accepted":False,"reason":"mock_rejected"},ensure_ascii=False))
    else: print(json.dumps({"accepted":True,"data":{"complete":{"description":"mock invalid payload"}}},ensure_ascii=False))
    raise SystemExit(0)
if outcome=="fact":
    label=prompt.get("intent_id") or phase; print(json.dumps({"accepted":True,"data":{"description":f"mock fact for {label}"}},ensure_ascii=False))
elif outcome=="rejected": print(json.dumps({"accepted":False,"reason":"mock_rejected"},ensure_ascii=False))
else: print(json.dumps({"accepted":True,"data":{}},ensure_ascii=False))
'''.strip()


class MockDriver(SeedSessionDriver):
    type_name = "pi"

    def local_binary(self) -> str | None:
        return sys.executable

    @staticmethod
    def _argv(worker: WorkerConfig, prompt: str) -> list[str]:
        behavior = resolve_mock_behavior(worker.name, worker.env)
        return [sys.executable, "-c", _SCRIPT, json.dumps(behavior, ensure_ascii=False), prompt]

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        outcomes = resolve_mock_behavior(worker.name, worker.env)["healthcheck"]["outcomes"]
        ok = random.random() < outcomes.get("ok", 0.0)
        return HealthResult(ok=ok, status=200 if ok else 503, detail="" if ok else "mock healthcheck fail")

    def describe_health(self, worker: WorkerConfig) -> str:
        return "mock in-process healthcheck"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        return DriverResult(argv=self._argv(worker, prompt), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> DriverResult:
        return DriverResult(argv=self._argv(worker, prompt), session=session)


def install_mock_pi_driver(monkeypatch: MonkeyPatch) -> MockDriver:
    driver = MockDriver()
    monkeypatch.setitem(worker_registry.DRIVERS, "pi", driver)
    monkeypatch.setitem(worker_registry.LOCAL_DRIVERS, "pi", driver)
    return driver
