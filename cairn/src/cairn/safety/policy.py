from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import posixpath
import re
import shlex
from typing import Any, Literal

from cairn.dispatcher.config import ResourceBudgetConfig


Decision = Literal["allow", "block", "resource_pause"]


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    decision: Decision
    rule_id: str | None
    reason: str
    target: str | None = None
    auth_attempt_count: int = 0


def classify_tool_call(
    tool_name: str,
    input_data: Mapping[str, Any],
    *,
    cwd: str,
    limits: ResourceBudgetConfig,
) -> SafetyDecision:
    if tool_name in {"write", "edit"}:
        protected = _classify_write_path(str(input_data.get("path", "")), cwd=cwd)
        if protected is not None:
            return protected

    if tool_name != "bash":
        return _allow()

    command = str(input_data.get("command", ""))
    invocations = _parse_invocations(command)
    for tokens in invocations:
        blocked = _classify_blocked_invocation(tokens)
        if blocked is not None:
            return blocked

    if _FORK_BOMB.search(command):
        return SafetyDecision("block", "resource_exhaustion", "explicit process bomb is prohibited")

    resource = _classify_resource_use(command, invocations, limits=limits)
    if resource is not None:
        return resource
    return _allow(auth_attempt_count=_count_auth_attempts(invocations))


def _allow(*, auth_attempt_count: int = 0) -> SafetyDecision:
    return SafetyDecision(
        "allow",
        None,
        "no_high_confidence_red_line",
        auth_attempt_count=auth_attempt_count,
    )


def _classify_write_path(path: str, *, cwd: str) -> SafetyDecision | None:
    if not path:
        return SafetyDecision("block", "protected_path_write", "write path is missing")
    normalized_cwd = _normalize_posix_path(cwd)
    normalized = _normalize_posix_path(path, cwd=normalized_cwd)
    protected = (
        normalized == "/var/run/docker.sock"
        or normalized.startswith("/proc/1/root/")
        or normalized == "/proc/1/root"
        or normalized.startswith("/etc/")
        or normalized == "/etc"
        or normalized.startswith("/var/log/")
        or normalized == "/var/log"
        or normalized.endswith("/.local/share/cairn/cairn.db")
        or "/safety/pi_extension/" in f"{normalized}/"
    )
    try:
        below_cwd = posixpath.commonpath([normalized_cwd, normalized]) == normalized_cwd
    except ValueError:
        below_cwd = False
    if protected or not below_cwd:
        return SafetyDecision(
            "block",
            "protected_path_write",
            "write or edit target is protected or outside the current workspace",
            target=normalized,
        )
    return None


def _normalize_posix_path(path: str, *, cwd: str | None = None) -> str:
    value = path.strip().replace("\\", "/")
    if value.startswith("~"):
        return posixpath.normpath(value)
    if not posixpath.isabs(value) and cwd is not None:
        value = posixpath.join(cwd, value)
    return posixpath.normpath(value)


def _parse_invocations(command: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        raw_tokens = list(lexer)
    except ValueError:
        return []

    invocations: list[list[str]] = []
    current: list[str] = []
    for token in raw_tokens:
        if token and set(token) <= {";", "&", "|"}:
            if current:
                invocations.append(current)
                current = []
            continue
        current.append(token)
    if current:
        invocations.append(current)
    return [tokens for tokens in (_unwrap_invocation(item) for item in invocations) if tokens]


def _unwrap_invocation(tokens: list[str]) -> list[str]:
    result = list(tokens)
    while result and ("=" in result[0] and not result[0].startswith(("-", "/"))):
        result.pop(0)
    while result and _basename(result[0]) in {"command", "env", "nohup", "sudo"}:
        wrapper = _basename(result.pop(0))
        while result and result[0].startswith("-"):
            option = result.pop(0)
            if wrapper == "sudo" and option in {"-u", "-g", "-h", "-p", "-C", "-T", "-r", "-t"} and result:
                result.pop(0)
        while result and ("=" in result[0] and not result[0].startswith(("-", "/"))):
            result.pop(0)
    return result


def _classify_blocked_invocation(tokens: list[str]) -> SafetyDecision | None:
    if not tokens:
        return None
    executable = _basename(tokens[0])
    args = tokens[1:]

    if executable == "rm" and _has_combined_flags(args, "r", "f"):
        targets = [arg for arg in args if not arg.startswith("-")]
        if any(_is_non_workspace_delete_target(target) for target in targets):
            return _block("filesystem_mass_delete", "recursive forced deletion outside the workspace", targets[-1] if targets else None)

    if executable == "dd":
        device_targets = [arg[3:] for arg in args if arg.startswith("of=/dev/")]
        if any(target not in {"/dev/null", "/dev/stdout", "/dev/stderr"} for target in device_targets):
            return _block("filesystem_device_overwrite", "raw device overwrite is prohibited", device_targets[-1])

    if executable in {"psql", "mysql", "mariadb", "sqlite3"}:
        statement = next((arg for arg in args if re.match(r"\s*(?:drop|truncate)\b", arg, re.IGNORECASE)), None)
        if statement is not None:
            return _block("database_destructive_statement", "destructive database statement is prohibited", statement)

    if executable == "systemctl" and any(arg.lower() in {"stop", "restart", "disable", "mask", "kill", "reboot", "poweroff", "halt"} for arg in args):
        return _block("service_disruption", "service disruption is prohibited", _last_argument(args))

    if executable in {"useradd", "adduser", "userdel", "deluser", "usermod", "groupadd", "groupdel", "groupmod"}:
        return _block("identity_mutation", "identity mutation is prohibited", _last_argument(args))

    if executable == "crontab" and args and args != ["-l"]:
        return _block("persistence_installation", "persistent scheduled task installation is prohibited", _last_argument(args))

    if executable == "journalctl" and any(arg.startswith("--vacuum-") for arg in args):
        return _block("audit_log_clearing", "audit log clearing is prohibited")

    if executable in {"hping", "hping2", "hping3"} and "--flood" in args:
        return _block("resource_exhaustion", "explicit flood mode is prohibited", _last_argument(args))

    if executable == "curl" and any(arg in {"-T", "--upload-file"} or arg.startswith("--upload-file=") for arg in args):
        return _block("explicit_file_upload", "explicit outbound file upload is prohibited", _last_argument(args))

    if executable == "curl" and _option_value(args, {"--unix-socket"}) == "/var/run/docker.sock":
        return _block("control_plane_access", "container control-plane access is prohibited", "/var/run/docker.sock")

    if executable == "pi" and any(arg in {"--no-extensions", "--no-extension"} for arg in args):
        return _block("safety_bypass", "disabling the trusted safety extension is prohibited")
    return None


def _classify_resource_use(
    command: str,
    invocations: list[list[str]],
    *,
    limits: ResourceBudgetConfig,
) -> SafetyDecision | None:
    if re.search(r"\b(?:for|while)\b[\s\S]*?\bdo\b[\s\S]*?&\s*\bdone\b", command):
        return SafetyDecision(
            "resource_pause",
            "background_worker_fanout",
            "background worker loop requires manual bounded execution",
        )

    for tokens in invocations:
        if not tokens:
            continue
        executable = _basename(tokens[0])
        args = tokens[1:]
        if executable == "timeout":
            seconds = _duration_seconds(args[0] if args else "")
            if seconds is not None and seconds > limits.max_unattended_bulk_seconds:
                return SafetyDecision(
                    "resource_pause",
                    "bulk_duration_limit",
                    f"unattended duration {seconds}s exceeds {limits.max_unattended_bulk_seconds}s",
                )

        concurrency = _bulk_concurrency(executable, args)
        if concurrency is not None and concurrency > limits.max_bulk_concurrency:
            return SafetyDecision(
                "resource_pause",
                "bulk_concurrency_limit",
                f"bulk concurrency {concurrency} exceeds {limits.max_bulk_concurrency}",
            )

        if executable in _AUTH_TOOLS:
            auth_concurrency = _auth_concurrency(args)
            if auth_concurrency is not None and auth_concurrency > limits.auth_concurrency:
                return SafetyDecision(
                    "resource_pause",
                    "auth_concurrency_limit",
                    f"authentication concurrency {auth_concurrency} exceeds {limits.auth_concurrency}",
                    auth_attempt_count=_count_auth_attempts(invocations),
                )
            has_unbounded_list = any(
                arg in {"-L", "-P", "-C"} or arg.startswith(("--user-list", "--pass-list"))
                for arg in args
            )
            if executable in {"netexec", "crackmapexec", "nxc"}:
                has_unbounded_list = has_unbounded_list or _has_credential_file_option(args)
            if has_unbounded_list:
                return SafetyDecision(
                    "resource_pause",
                    "auth_batch_unbounded",
                    "credential list size is not explicitly bounded",
                )

    auth_attempt_count = _count_auth_attempts(invocations)
    if auth_attempt_count > limits.auth_attempts_per_batch:
        return SafetyDecision(
            "resource_pause",
            "auth_batch_limit",
            f"credential attempts {auth_attempt_count} exceed batch limit {limits.auth_attempts_per_batch}",
            auth_attempt_count=auth_attempt_count,
        )
    return None


def _bulk_concurrency(executable: str, args: list[str]) -> int | None:
    if executable == "xargs":
        return _integer_option(args, short={"-P"}, long={"--max-procs"})
    if executable == "parallel":
        return _integer_option(args, short={"-j"}, long={"--jobs"})
    if executable == "nuclei":
        return _integer_option(args, short={"-c"}, long={"--concurrency"})
    return _integer_option(
        args,
        short=set(),
        long={"--jobs", "--threads", "--concurrency", "--parallel", "--parallel-max"},
    )


def _auth_concurrency(args: list[str]) -> int | None:
    return _integer_option(args, short={"-t", "-T"}, long={"--tasks", "--threads", "--concurrency"})


def _integer_option(args: list[str], *, short: set[str], long: set[str]) -> int | None:
    for index, arg in enumerate(args):
        if arg in short | long and index + 1 < len(args):
            return _positive_int(args[index + 1])
        for option in long:
            if arg.startswith(f"{option}="):
                return _positive_int(arg.split("=", 1)[1])
        for option in short:
            if arg.startswith(option) and arg != option:
                return _positive_int(arg[len(option) :])
    return None


def _positive_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _duration_seconds(value: str) -> int | None:
    match = re.fullmatch(r"(\d+)([smh]?)", value.lower())
    if match is None:
        return None
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]
    return int(match.group(1)) * multiplier


def _count_auth_attempts(invocations: list[list[str]]) -> int:
    count = 0
    for tokens in invocations:
        if not tokens or _basename(tokens[0]) not in _AUTH_TOOLS:
            continue
        args = tokens[1:]
        if _has_option(args, "-l", "--login") and _has_option(args, "-p", "--password"):
            count += 1
        elif _basename(tokens[0]) in {"netexec", "crackmapexec", "nxc"} and _has_option(args, "-u") and _has_option(args, "-p"):
            count += 1
        elif _basename(tokens[0]) == "sshpass" and _has_option(args, "-p"):
            count += 1
    return count


def _has_option(args: list[str], *options: str) -> bool:
    return any(arg in options or any(arg.startswith(f"{option}=") for option in options if option.startswith("--")) for arg in args)


def _option_value(args: list[str], options: set[str]) -> str | None:
    for index, arg in enumerate(args):
        if arg in options and index + 1 < len(args):
            return args[index + 1]
        for option in options:
            if arg.startswith(f"{option}="):
                return arg.split("=", 1)[1]
    return None


def _has_credential_file_option(args: list[str]) -> bool:
    for index, arg in enumerate(args[:-1]):
        if arg not in {"-u", "-p", "--username", "--password"}:
            continue
        value = args[index + 1].lower()
        if value.endswith((".txt", ".lst", ".csv", ".json")):
            return True
    return False


def _has_combined_flags(args: list[str], *required: str) -> bool:
    flags = "".join(arg[1:] for arg in args if arg.startswith("-") and not arg.startswith("--"))
    long_flags = set(args)
    return all(flag in flags or {"r": "--recursive", "f": "--force"}.get(flag) in long_flags for flag in required)


def _is_non_workspace_delete_target(target: str) -> bool:
    normalized = target.replace("\\", "/")
    return normalized.startswith(("/", "~", "../")) or normalized in {".", ".."}


def _block(rule_id: str, reason: str, target: str | None = None) -> SafetyDecision:
    return SafetyDecision("block", rule_id, reason, target=target)


def _last_argument(args: list[str]) -> str | None:
    values = [arg for arg in args if not arg.startswith("-")]
    return values[-1] if values else None


def _basename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].lower()


_AUTH_TOOLS = frozenset({"hydra", "medusa", "ncrack", "netexec", "crackmapexec", "nxc", "sshpass"})
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")
