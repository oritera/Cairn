"""Entry point for the Cairn desktop app (``python -m cairn.desktop`` / ``Cairn.exe``).

The packaged ``Cairn.exe`` also stands in for Python and the CLI for its own child processes,
because a frozen app has no separate interpreter:

* ``Cairn.exe dispatch --config -``  runs the dispatcher (how the app starts it)
* ``Cairn.exe serve ...``            runs the API server alone
* ``Cairn.exe -c "<code>" args...``  runs code like ``python -c`` (the mock worker uses this)
* ``Cairn.exe --help / --version``   prints and exits (used by the dispatcher's CLI probe)

Anything else opens the desktop window.
"""

from __future__ import annotations

import sys

CLI_COMMANDS = {"serve", "dispatch"}


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in CLI_COMMANDS:
        from cairn.cli import main as cli_main

        cli_main(args=args, prog_name="cairn")
        return
    if args and args[0] == "-c" and len(args) >= 2:
        _run_code(args[1], args[2:])
        return
    if args and args[0] in ("--help", "-h", "--version", "-V"):
        _print_info(args[0])
        return
    if args and args[0] == "--purge-secrets":
        _purge_secrets()
        return
    from cairn.desktop.app import run

    run()


def _run_code(code: str, rest: list[str]) -> None:
    """Behave like ``python -c``: sys.argv[0] is '-c', the rest follow."""
    from cairn.dispatcher.runtime.platform_compat import attach_missing_stdio

    attach_missing_stdio()
    sys.argv = ["-c", *rest]
    namespace = {"__name__": "__main__"}
    exec(compile(code, "<string>", "exec"), namespace)  # noqa: S102 - same contract as python -c


def _purge_secrets() -> None:
    """Delete every API key this app stored in Credential Manager (used by the uninstaller)."""
    from cairn.desktop import paths
    from cairn.desktop.settings import KeyringSecretStore, SettingsStore

    store = SettingsStore(paths.settings_file(), KeyringSecretStore(), paths.default_workspace_root())
    for secret_id in store.load().get("secret_refs", []):
        try:
            store.secrets.delete(secret_id)
        except Exception:
            pass


def _print_info(flag: str) -> None:
    from cairn import __version__
    from cairn.dispatcher.runtime.platform_compat import attach_missing_stdio

    attach_missing_stdio()
    if sys.stdout is None:
        return
    if flag in ("--version", "-V"):
        print(f"Cairn {__version__}")
    else:
        print("Cairn desktop app. Run without arguments to open the window.\n"
              "Sub-commands: serve, dispatch (same options as the `cairn` CLI).")


if __name__ == "__main__":
    main()
