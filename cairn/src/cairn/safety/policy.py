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
    redirected = _classify_redirection(command, cwd=cwd)
    if redirected is not None:
        return redirected
    raw_invocations = _expand_nested_shells(_parse_invocations(command, unwrap=False))
    invocations = _expand_nested_shells(_parse_invocations(command))
    for tokens in invocations:
        blocked = _classify_blocked_invocation(tokens)
        if blocked is not None:
            return blocked

    if _FORK_BOMB.search(command):
        return SafetyDecision("block", "resource_exhaustion", "explicit process bomb is prohibited")

    resource = _classify_resource_use(command, invocations, limits=limits, raw_invocations=raw_invocations)
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
    protected = _is_protected_path(normalized) or "/safety/pi_extension/" in f"{normalized}/"
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


def _parse_invocations(command: str, *, unwrap: bool = True) -> list[list[str]]:
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
    if not unwrap:
        # The wrapper itself carries limits the inner command does not: "timeout 900 nmap" and
        # "xargs -P 8 curl" are only visible before unwrapping, and "timeout 5 rm -rf /etc" is
        # only visible after. Both views are needed, so both are returned to the caller.
        return [list(tokens) for tokens in invocations if tokens]
    return [tokens for tokens in (_unwrap_invocation(item) for item in invocations) if tokens]


# Wrappers that run another command as their trailing argument. Anything not listed here keeps
# the wrapper as the executable, which is how "timeout 5 rm -rf /etc" and "busybox rm -rf /etc"
# used to reach the classifier as if they were harmless.
_WRAPPERS = frozenset({
    "command", "env", "nohup", "sudo", "doas", "timeout", "nice", "ionice", "setsid",
    "stdbuf", "watch", "xargs", "busybox", "toybox", "chrt", "taskset", "script",
})
# Wrappers whose option values must be skipped with the option (name -> options taking a value).
_WRAPPER_VALUE_OPTIONS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-T", "-r", "-t"},
    "doas": {"-u", "-C"},
    "timeout": {"-k", "--kill-after", "-s", "--signal"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p"},
    "stdbuf": {"-i", "-o", "-e"},
    "xargs": {"-n", "-P", "-I", "-s", "-E", "-d"},
    "taskset": {"-c", "-p"},
    "chrt": {"-p"},
}
# Wrappers that take a leading duration/signal operand before the command.
_WRAPPER_LEADING_OPERAND = {"timeout": re.compile(r"^\d+[smhd]?$")}


def _unwrap_invocation(tokens: list[str]) -> list[str]:
    result = list(tokens)
    while result and ("=" in result[0] and not result[0].startswith(("-", "/"))):
        result.pop(0)
    while result and _basename(result[0]) in _WRAPPERS:
        wrapper = _basename(result.pop(0))
        while result and result[0].startswith("-"):
            option = result.pop(0)
            name = option.split("=", 1)[0]
            if name in _WRAPPER_VALUE_OPTIONS.get(wrapper, set()) and "=" not in option and result:
                result.pop(0)
        pattern = _WRAPPER_LEADING_OPERAND.get(wrapper)
        if pattern is not None and result and pattern.match(result[0]):
            result.pop(0)
        while result and ("=" in result[0] and not result[0].startswith(("-", "/"))):
            result.pop(0)
    return result


# Redirection targets are matched against the raw command rather than the token list: shlex
# keeps ">" attached to its target ("echo x>/etc/hosts" is one token), so token scanning
# misses exactly the compact form an evasive caller would use.
_REDIRECTION = re.compile(r"""(?:>>?|&>>?|\d>>?)\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")

# Paths whose mutation is a red line, whichever channel reaches them: the write/edit tools, a
# shell redirection, or an argument-style mutator (cp/mv/tee/sed -i/truncate/chmod/...). One
# set for every channel, so the channels cannot drift apart.
# The "outside the workspace" rule is deliberately NOT part of this set: "> /tmp/out" and
# "2>/dev/null" are ordinary, and applying the workspace boundary to every channel would block
# them in bulk (the file tools still enforce it, where cwd semantics are precise).
_HARD_PROTECTED = frozenset({
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/proc/1/root",
    "/proc/sysrq-trigger",
    "/etc",
    "/var/log",
    "/root",
})
_HARD_PROTECTED_PREFIXES = (
    "/proc/1/root/",
    "/proc/sys/",
    "/etc/",
    "/var/log/",
    "/var/spool/cron/",
    "/run/docker.sock",
    "/safety/pi_extension/",
    "/sys/",
    "/root/",
)
# Persistence and credential surfaces that do not sit under the prefixes above.
_HARD_PROTECTED_SUFFIXES = (
    "/.ssh/authorized_keys",
    "/.ssh/config",
    "/.bashrc",
    "/.bash_profile",
    "/.bash_login",
    "/.profile",
    "/.zshrc",
    "/etc/ld.so.preload",
    "/etc/rc.local",
    "/etc/sudoers",
    "/.local/share/cairn/cairn.db",
)

# The sandbox user's home. The worker image creates "kali" with -m, and the workspace is
# /home/kali/workspace, so ~-relative persistence paths resolve under this prefix.
_SANDBOX_HOME = "/home/kali"


def _is_protected_path(normalized: str) -> bool:
    if normalized in _HARD_PROTECTED:
        return True
    if normalized.startswith(_HARD_PROTECTED_PREFIXES):
        return True
    return normalized.endswith(_HARD_PROTECTED_SUFFIXES)


def _expand_target(target: str) -> str:
    """Normalise a path token before matching it against the protected set.

    Quotes are stripped ("> '/etc/hosts'" must not slip through) and ~, $HOME and ${HOME} are
    expanded, because the token list carries them as written while the protected set is
    absolute.
    """
    value = target.strip()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    for prefix in ("${HOME}", "$HOME"):
        if value.startswith(prefix):
            value = _SANDBOX_HOME + value[len(prefix):]
            break
    if value == "~" or value.startswith("~/"):
        value = _SANDBOX_HOME + value[1:]
    return value


def _classify_redirection(command: str, *, cwd: str) -> SafetyDecision | None:
    """Apply the protected-path rule to shell redirections.

    The write/edit tools pass through _classify_write_path; a shell redirection reaches the
    same paths without ever touching that gate ("echo x > /etc/hosts", "> /safety/..."), so
    the same protected set is checked here.
    """
    normalized_cwd = _normalize_posix_path(cwd)
    for match in _REDIRECTION.finditer(command):
        target = next((group for group in match.groups() if group), "")
        if not target or target in {"/dev/null", "/dev/stdout", "/dev/stderr"}:
            continue
        normalized = _normalize_posix_path(_expand_target(target), cwd=normalized_cwd)
        if _is_protected_path(normalized):
            return SafetyDecision(
                "block",
                "protected_path_write",
                "shell redirection targets a protected path",
                target=normalized,
            )
    return None


def _expand_nested_shells(invocations: list[list[str]], *, depth: int = 0) -> list[list[str]]:
    """Splice in the payload of `sh -c '<command>'` wrappers.

    Without this a destructive statement hidden in a shell string ("bash -c 'mysql -e ...'")
    is classified as an ordinary bash invocation and allowed. Depth is bounded because a
    payload can itself contain a nested shell string.
    """
    if depth >= 3:
        return list(invocations)
    expanded: list[list[str]] = []
    for tokens in invocations:
        expanded.append(tokens)
        payload = _shell_payload(tokens)
        if payload is not None:
            expanded.extend(_expand_nested_shells(_parse_invocations(payload), depth=depth + 1))
    return expanded


def _shell_payload(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    executable = _basename(tokens[0])
    if executable not in _SHELLS and executable not in _C_SHELLS:
        return None
    for index, arg in enumerate(tokens[1:], start=1):
        if arg == "--":
            break
        if arg == "-c" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]):
            if index + 1 < len(tokens):
                return tokens[index + 1]
    return None


def _positional(args: list[str]) -> list[str]:
    return [arg for arg in args if arg and not arg.startswith("-")]


def _rm_is_recursive_or_forced(args: list[str]) -> bool:
    """Recursive OR forced, case-insensitively.

    The previous check required r AND f in one lower-case cluster, so "rm -r /etc",
    "rm -f /var/log/x" and even "rm -Rf /etc" all passed the gate.
    """
    letters = "".join(arg[1:] for arg in args if arg.startswith("-") and not arg.startswith("--"))
    if any(letter in letters for letter in "rRfF"):
        return True
    return any(arg in {"--recursive", "--force"} for arg in args)


def _protected_mutation_target(executable: str, args: list[str]) -> str | None:
    """The protected path an argument-style writer would touch, if any.

    ALL_ARGS writers name a path in every positional argument; DESTINATION writers are checked
    on the destination only, because reading a protected source is not a red line. mv and the
    in-place editors are checked on every path argument: they also remove or rewrite the source.
    """
    if executable in _MUTATORS_ALL_ARGS or executable == "mv":
        candidates = _positional(args)
    elif executable in _MUTATORS_DESTINATION:
        last = _last_argument(args)
        candidates = [last] if last else []
    elif executable == "sed":
        if not any(arg == "-i" or arg.startswith("-i") or arg.startswith("--in-place") for arg in args):
            return None
        candidates = _positional(args)
    elif executable == "dd":
        candidates = [arg[3:] for arg in args if arg.startswith("of=")]
    else:
        return None
    for candidate in candidates:
        normalized = _normalize_posix_path(_expand_target(candidate))
        if _is_protected_path(normalized):
            return normalized
    return None


def _crontab_mutates(args: list[str]) -> bool:
    """crontab -l only lists, including with -u USER; everything else installs or edits."""
    if "-l" not in args:
        return True
    if any(arg in {"-e", "-r", "-i"} for arg in args):
        return True
    for index, arg in enumerate(args):
        if index and args[index - 1] in {"-u", "--user"}:
            continue
        if not arg.startswith("-"):
            return True
    return False


def _targets_init_process(args: list[str]) -> bool:
    return any(arg == "1" for arg in args)


def _curl_exfiltrates(args: list[str]) -> bool:
    """Any curl form that reads a local file and sends it: -T, -Tfile, -d @file, -F k=@file.

    The sanctioned verification payload is exempt (see _is_sanctioned_upload).
    """
    for index, arg in enumerate(args):
        if arg.startswith("-T") and arg != "-T":
            operand = arg[2:]
            if _is_sanctioned_upload(operand):
                continue
            return True
        if arg in {"-T", "--upload-file"} or arg.startswith("--upload-file="):
            value = arg.split("=", 1)[1] if "=" in arg else (args[index + 1] if index + 1 < len(args) else "")
            if _is_sanctioned_upload(value):
                continue
            return True
        name = arg.split("=", 1)[0]
        if name not in {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "-F", "--form"}:
            continue
        value = arg.split("=", 1)[1] if "=" in arg else (args[index + 1] if index + 1 < len(args) else "")
        if value.startswith("@"):
            if _is_sanctioned_upload(value):
                continue
            return True
        if name in {"-F", "--form"} and "@" in value:
            if _is_sanctioned_upload(value.split("@", 1)[1]):
                continue
            return True
    return False


_REMOTE_SPEC = re.compile(r"^(?:[^/@]*@)?[^/:]+:")

# The engagement rules permit ONE sanitised upload verification. That payload is defined as an
# inert byte sequence under a fresh random name with a non-consumable extension, so a local file
# is allowed to be sent only when it matches this shape. Everything else -- target data, dumps,
# credentials, system files -- stays classified as outbound exfiltration.
_SANCTIONED_UPLOAD = re.compile(
    r"^cairn-upload-[0-9a-fA-F][0-9a-fA-F-]{7,}\.(?:bin|dat|blob|txt|log|json)$"
)


def _is_sanctioned_upload(value: str) -> bool:
    """True when the operand is the engagement's inert verification payload.

    Accepts either a bare file name or a path to it; only the base name is matched, because the
    rule is about the payload's shape, not the directory it happens to sit in.
    """
    candidate = value.strip().strip("'\"")
    if candidate.startswith("@"):
        candidate = candidate[1:]
    name = candidate.replace("\\", "/").rsplit("/", 1)[-1]
    return _SANCTIONED_UPLOAD.match(name) is not None


def _remote_destination(args: list[str]) -> bool:
    """scp/rsync/sftp only exfiltrate when the destination is the remote side."""
    positional = _positional(args)
    if len(positional) < 2:
        return False
    return _REMOTE_SPEC.match(positional[-1]) is not None


def _classify_blocked_invocation(tokens: list[str]) -> SafetyDecision | None:
    if not tokens:
        return None
    executable = _basename(tokens[0])
    args = tokens[1:]

    # --- safety bypass: any agent runtime other than the trusted Pi extension ------------
    # The gate is wired into Pi through its extension API. Any other agent CLI that happens to
    # be installed (the upstream worker image ships codex and claude-code alongside pi) runs
    # its own tool loop with no preflight, no audit and no block, so invoking one is a bypass
    # of the safety layer rather than an ordinary command.
    if executable in _FOREIGN_AGENT_CLIS:
        return _block(
            "safety_bypass",
            "running an agent CLI without the trusted safety extension is prohibited",
            executable,
        )
    if executable == "pi" and not any("cairn-safety" in arg for arg in args):
        return _block("safety_bypass", "launching Pi without the trusted safety extension is prohibited")

    # --- container control plane --------------------------------------------------------
    if executable in _CONTAINER_CLIENTS or executable in _NAMESPACE_TOOLS:
        return _block("control_plane_access", "container control-plane access is prohibited", executable)
    if executable == "curl" and any(arg == "--unix-socket" or arg.startswith("--unix-socket=") for arg in args):
        return _block("control_plane_access", "unix socket access is prohibited", _option_value(args, {"--unix-socket"}))

    # --- host and service disruption ----------------------------------------------------
    if executable in _SHUTDOWN_EXECUTABLES:
        return _block("service_disruption", "host shutdown or reboot is prohibited")
    if executable in {"init", "telinit"} and any(arg in {"0", "6", "s", "S", "q", "Q"} for arg in args):
        return _block("service_disruption", "host shutdown or reboot is prohibited")
    if executable == "systemctl" and any(arg.lower() in _SYSTEMCTL_DISRUPTIVE for arg in args):
        return _block("service_disruption", "service disruption is prohibited", _last_argument(args))
    if executable == "service" and any(arg.lower() in _SERVICE_ACTIONS for arg in args):
        return _block("service_disruption", "service disruption is prohibited", _last_argument(args))
    if executable == "kubectl" and args and args[0].lower() in _KUBECTL_DISRUPTIVE:
        return _block("service_disruption", "cluster disruption is prohibited", args[0])
    if executable in {"kill", "pkill", "killall"} and _targets_init_process(args):
        return _block("service_disruption", "killing the init process is prohibited")

    # --- filesystem and block-device destruction ----------------------------------------
    if executable in _DEVICE_EXECUTABLES or executable.startswith("mkfs"):
        return _block("filesystem_device_overwrite", "filesystem creation or device wiping is prohibited", _last_argument(args))
    if executable == "dd":
        device_targets = [arg[3:] for arg in args if arg.startswith("of=/dev/")]
        if any(target not in {"/dev/null", "/dev/stdout", "/dev/stderr"} for target in device_targets):
            return _block("filesystem_device_overwrite", "raw device overwrite is prohibited", device_targets[-1])
    if executable == "find":
        if "-delete" in args and any(
            _is_non_workspace_delete_target(_expand_target(target)) for target in _positional(args)
        ):
            return _block("filesystem_mass_delete", "recursive deletion outside the workspace", _last_argument(args))
        for index, arg in enumerate(args):
            if arg in {"-exec", "-execdir", "-ok", "-okdir"}:
                payload = " ".join(args[index + 1:])
                if _DELEGATED_DESTRUCTION.search(payload):
                    return _block("filesystem_mass_delete", "delegated deletion or destruction is prohibited", payload)

    if executable == "rm" and _rm_is_recursive_or_forced(args):
        targets = _positional(args)
        if any(_is_non_workspace_delete_target(_expand_target(target)) for target in targets):
            return _block("filesystem_mass_delete", "recursive or forced deletion outside the workspace", targets[-1] if targets else None)

    # --- destructive data statements -----------------------------------------------------
    if executable in _SQL_CLIENTS:
        statement = _destructive_sql_argument(args)
        if statement is not None:
            return _block("database_destructive_statement", "destructive database statement is prohibited", statement)
    if executable in _NOSQL_CLIENTS:
        statement = _destructive_nosql_argument(args)
        if statement is not None:
            return _block("database_destructive_statement", "destructive data-store command is prohibited", statement)

    # --- identity, credential and ACL mutation -------------------------------------------
    if executable in _IDENTITY_EXECUTABLES:
        return _block("identity_mutation", "identity mutation is prohibited", _last_argument(args))
    if executable in {"setfacl", "setfattr"} and any(arg == "-m" or arg.startswith("--modify") for arg in args):
        return _block("identity_mutation", "ACL mutation is prohibited", _last_argument(args))

    # --- persistence ----------------------------------------------------------------------
    if executable == "crontab" and _crontab_mutates(args):
        return _block("persistence_installation", "persistent scheduled task installation is prohibited", _last_argument(args))
    if executable in _PERSISTENCE_EXECUTABLES:
        return _block("persistence_installation", "persistence installation is prohibited", _last_argument(args))
    if executable == "systemctl" and args and args[0].lower() in _SYSTEMCTL_PERSISTENT:
        return _block("persistence_installation", "persistent unit enablement is prohibited", args[0])

    # --- audit log clearing ----------------------------------------------------------------
    if executable == "journalctl" and any(arg.startswith("--vacuum-") or arg == "--rotate" for arg in args):
        return _block("audit_log_clearing", "audit log clearing is prohibited")
    if executable == "logrotate" and any(arg in {"-f", "--force"} for arg in args):
        return _block("audit_log_clearing", "forced log rotation is prohibited")

    # --- denial of service -------------------------------------------------------------------
    if executable in _FLOOD_FLAG_TOOLS and any(arg in _FLOOD_FLAGS for arg in args):
        return _block("resource_exhaustion", "explicit flood mode is prohibited", _last_argument(args))
    if executable in _LOAD_TOOLS:
        return _block("resource_exhaustion", "load generation is prohibited", executable)
    if _FORK_BOMB.search(" ".join(tokens)):
        return _block("resource_exhaustion", "explicit process bomb is prohibited")

    # --- outbound file transfer ---------------------------------------------------------------
    if executable == "curl" and _curl_exfiltrates(args):
        return _block("explicit_file_upload", "explicit outbound file upload is prohibited", _last_argument(args))
    if executable == "wget":
        for index, arg in enumerate(args):
            if not arg.startswith(("--post-file", "--body-file")):
                continue
            value = arg.split("=", 1)[1] if "=" in arg else (args[index + 1] if index + 1 < len(args) else "")
            if not _is_sanctioned_upload(value):
                return _block("explicit_file_upload", "explicit outbound file upload is prohibited", _last_argument(args))
    if executable in {"scp", "rsync", "sftp"} and _remote_destination(args):
        return _block("explicit_file_upload", "outbound copy to a remote host is prohibited", _last_argument(args))

    # --- protected-path mutation through any argument-style writer ----------------------------
    mutated = _protected_mutation_target(executable, args)
    if mutated is not None:
        return _block("protected_path_write", "write or edit target is protected", target=mutated)

    return None


def _classify_resource_use(
    command: str,
    invocations: list[list[str]],
    *,
    limits: ResourceBudgetConfig,
    raw_invocations: list[list[str]] | None = None,
) -> SafetyDecision | None:
    if re.search(r"\b(?:for|while)\b[\s\S]*?\bdo\b[\s\S]*?&\s*\bdone\b", command):
        return SafetyDecision(
            "resource_pause",
            "background_worker_fanout",
            "background worker loop requires manual bounded execution",
        )

    for tokens in [*(raw_invocations or []), *invocations]:
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
            # A credential list is only unbounded *relative to the declared rate*. When the
            # invocation already declares the engagement's authentication concurrency (1), the
            # rules permit the low-rate sampling pass, and the per-batch counter below still
            # bounds total attempts. Without that declaration the list is treated as unbounded.
            declares_low_rate = (
                auth_concurrency is not None and auth_concurrency <= limits.auth_concurrency
            )
            if has_unbounded_list and not declares_low_rate:
                return SafetyDecision(
                    "resource_pause",
                    "auth_batch_unbounded",
                    "credential list size is not explicitly bounded; declare low-rate concurrency "
                    f"(-t {limits.auth_concurrency}) to run a sampled validation pass",
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
    """Absolute, home-relative, parent-relative, self, or a glob -- all batch deletion."""
    normalized = target.replace("\\", "/")
    if normalized.startswith(("/", "~", "../")):
        return True
    if normalized in {".", "..", "./*", ".*"}:
        return True
    return "*" in normalized


def _block(rule_id: str, reason: str, target: str | None = None) -> SafetyDecision:
    return SafetyDecision("block", rule_id, reason, target=target)


def _last_argument(args: list[str]) -> str | None:
    values = [arg for arg in args if not arg.startswith("-")]
    return values[-1] if values else None


def _basename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].lower()


# Destructive SQL: the keyword must open a statement (start, or after a ; separator) and be
# followed by whitespace. That second condition is what keeps a file name such as drop.sql and
# a quoted string such as select 'a drop b' from being read as statements, while still catching
# "SELECT 1; DROP TABLE users".
_DESTRUCTIVE_SQL = re.compile(
    r"(?:^|[;|])\s*(?:drop|truncate|delete\s+from|alter|create|insert\s+into|update|"
    r"replace\s+into|merge\s+into|grant|revoke|rename|lock\s+tables?|purge)\b(?=\s)",
    re.IGNORECASE,
)

# Redis / Mongo style stores: the command is a token rather than a statement.
_DESTRUCTIVE_NOSQL = re.compile(
    r"(?:^|[;|\s.])(?:flushall|flushdb|shutdown|config\s+set|replicaof|slaveof|del|unlink|"
    r"rename|restore|bgrewriteaof|script|dropdatabase|drop|deleteone|deletemany|remove|"
    r"updateone|updatemany|insertone|insertmany|replaceone|createindex|dropindex)\b",
    re.IGNORECASE,
)

# Short options can carry their value in the same token ("-eDROP TABLE t"), long options may be
# "--execute=DROP TABLE t". Both used to slip past a check that only looked at bare arguments.
_ATTACHED_SHORT_OPTION = re.compile(r"^-[A-Za-z]+")


def _destructive_sql_argument(args: list[str]) -> str | None:
    for arg in args:
        if not isinstance(arg, str) or not arg:
            continue
        candidate = arg
        if candidate.startswith("-"):
            if "=" in candidate:
                candidate = candidate.split("=", 1)[1]
            else:
                candidate = _ATTACHED_SHORT_OPTION.sub("", candidate)
        if candidate and _DESTRUCTIVE_SQL.search(candidate):
            return candidate
    return None


def _destructive_nosql_argument(args: list[str]) -> str | None:
    for arg in args:
        if not isinstance(arg, str) or not arg:
            continue
        candidate = arg.split("=", 1)[1] if (arg.startswith("-") and "=" in arg) else _ATTACHED_SHORT_OPTION.sub("", arg)
        if candidate and _DESTRUCTIVE_NOSQL.search(candidate):
            return candidate
    return None

_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh", "busybox", "toybox"})
# Not shells, but they take a whole command as their -c argument: the same recursion applies.
_C_SHELLS = frozenset({"su", "doas", "runuser", "pkexec"})

# ---------------------------------------------------------------------------------------
# Red-line classes. Each table maps a red line to the executables that can carry it out, so
# covering a new spelling of the same behaviour is a one-line addition rather than a new
# branch, and the rule covers the class instead of the one command that happened to be tested.
# ---------------------------------------------------------------------------------------

# Host / service disruption.
_SHUTDOWN_EXECUTABLES = frozenset({"reboot", "shutdown", "halt", "poweroff"})
_SYSTEMCTL_DISRUPTIVE = frozenset({
    "stop", "restart", "disable", "mask", "kill", "reboot", "poweroff", "halt", "emergency", "rescue",
})
_SERVICE_ACTIONS = frozenset({"stop", "restart", "reload", "force-reload", "disable", "mask", "kill"})
_SYSTEMCTL_PERSISTENT = frozenset({"enable", "re-enable", "link", "preset", "preset-all", "edit"})
_KUBECTL_DISRUPTIVE = frozenset({"delete", "drain", "cordon", "scale", "evict"})

# Agent CLIs that run their own tool loop. None of them loads the Cairn safety extension, so an
# invocation escapes the preflight gate entirely. Kept as a class so a new runtime in a future
# image is one line away from being covered.
_FOREIGN_AGENT_CLIS = frozenset({
    "claude", "codex", "aider", "cursor-agent", "gemini", "opencode", "goose", "crush", "amp",
})

# Container control plane. The worker image ships no container runtime and the sandbox has no
# daemon socket, so any invocation at all is a control-plane attempt.
_CONTAINER_CLIENTS = frozenset({"docker", "podman", "nerdctl", "ctr", "crictl", "buildah", "skopeo"})
_NAMESPACE_TOOLS = frozenset({"nsenter", "unshare", "chroot", "setns"})

# Filesystem / block-device destruction.
_DEVICE_EXECUTABLES = frozenset({
    "wipefs", "blkdiscard", "fdisk", "sfdisk", "parted", "sgdisk", "cfdisk", "mkswap",
    "badblocks", "hdparm", "shred", "pvcreate", "vgremove", "lvremove", "zpool",
})

# Identity and credential mutation.
_IDENTITY_EXECUTABLES = frozenset({
    "useradd", "adduser", "userdel", "deluser", "usermod",
    "groupadd", "addgroup", "groupdel", "delgroup", "groupmod",
    "passwd", "chpasswd", "gpasswd", "chsh", "chfn", "chage", "visudo", "vipw",
})

# Persistence.
_PERSISTENCE_EXECUTABLES = frozenset({
    "at", "batch", "atd", "systemd-run", "update-rc.d", "rc-update", "chkconfig",
    "insserv", "start-stop-daemon",
})

# Argument-style writers: commands that reach the same protected paths as write/edit and shell
# redirection. ALL_ARGS writers name a path in every positional argument; DESTINATION writers
# only write the destination, so reading a protected source is not a red line.
_MUTATORS_ALL_ARGS = frozenset({
    "rm", "rmdir", "unlink", "shred", "truncate", "srm", "tee", "chmod", "chown", "chgrp", "touch",
})
_MUTATORS_DESTINATION = frozenset({"cp", "install", "ln", "rsync", "scp"})

# Denial of service.
_FLOOD_FLAG_TOOLS = frozenset({"ping", "ping6", "hping", "hping2", "hping3"})
_FLOOD_FLAGS = frozenset({"-f", "--flood"})
_LOAD_TOOLS = frozenset({"ab", "wrk", "siege", "slowhttptest", "h2load", "stress", "stress-ng", "iperf", "iperf3"})

# Data stores.
_SQL_CLIENTS = frozenset({
    "psql", "mysql", "mariadb", "sqlite3", "sqlcmd", "mssql-cli", "sqlplus",
    "clickhouse-client", "cqlsh", "duckdb", "trino", "presto",
})
_NOSQL_CLIENTS = frozenset({"mongo", "mongosh", "redis-cli"})

_AUTH_TOOLS = frozenset({"hydra", "medusa", "ncrack", "netexec", "crackmapexec", "nxc", "sshpass"})
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")

# Commands delegated through find -exec: the inner command never becomes its own invocation.
_DELEGATED_DESTRUCTION = re.compile(r"\b(?:rm|shred|truncate|dd|mkfs\w*|wipefs)\b")
