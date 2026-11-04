# Cairn for Windows

> **This is an unofficial Windows port of [Cairn](https://github.com/oritera/Cairn) by
> [oritera](https://github.com/oritera).** All credit for Cairn itself (the engine, the server,
> the web UI and the dispatcher) goes to the original authors. This fork only adds Windows
> support and a desktop app, and is not affiliated with or endorsed by the upstream project.
> Report Windows-specific problems here, not upstream. The original README is kept as
> [README-UPSTREAM.md](README-UPSTREAM.md). Like the original, this port is licensed under the
> [GNU AGPL v3](LICENSE).

This port lets Cairn run on Windows. The server, the web UI and the dispatcher
run natively on Windows 10/11. Workers run in **local mode**: each worker is an ordinary
Windows process that uses the `claude` / `codex` / `pi` CLIs already installed on your PC.
You don't need Docker or WSL.

Container mode (one Linux container per project) also works if you have
[Docker Desktop](https://www.docker.com/products/docker-desktop/) with the WSL 2 backend.
See [Container mode on Windows](#container-mode-on-windows).

## Desktop app (installer)

The easiest way to use Cairn on Windows is the desktop app: one window with the Cairn UI, a
**Settings** window for API keys and every config value, and a proper installer.

**Download:** get `Cairn-Setup-<version>.exe` from the repository's [Releases](../../releases) page (tagged versions, plus a rolling `windows-latest` pre-release built from `main`), or from the artifacts of the *Build Windows Installer* workflow run under *Actions*.

**Install:** run `Cairn-Setup-<version>.exe`. It installs for your user only (no admin
rights needed) into `%LOCALAPPDATA%\Programs\Cairn`, adds a Start menu entry (and, if you tick
the box, a desktop shortcut), and registers an uninstaller in *Settings → Apps*.

**First launch:** the Settings window opens with a welcome note. Check the projects folder and
your worker, then click **Save & start**. With Claude Code installed and logged in, the
defaults work as they are.

What the app does for you:

- It runs the Cairn server inside the app (on `127.0.0.1`, port 8000 or the next free port)
  and shows the UI in a native window (Microsoft Edge WebView2).
- It starts, stops and restarts the dispatcher for you. The window title shows its state:
  *running*, *stopped*, or *stopped unexpectedly*, and if it stops unexpectedly the app shows
  the last log lines and offers to open Settings.
- **API keys are stored in Windows Credential Manager** (under “Cairn”), never in a file. Any
  environment variable whose name contains `KEY`, `TOKEN`, `SECRET` or `PASSWORD` is treated
  as a secret: the field is masked, it shows “✓ Saved” once stored, and it is never sent
  back to the window. Keys reach the dispatcher only in memory (over stdin).
- Settings are checked before they are saved. Invalid settings are not written, and the
  window lists what to fix.
- **Test** checks each worker: it confirms the CLI is installed (and shows its version), and
  if API settings are filled in, it calls the API to check the key.

| Menu | Items |
|---|---|
| **Cairn** | Settings… · Import dispatch.yaml… (moves its keys into Credential Manager) · Open projects folder · Open data folder · Quit |
| **Dispatcher** | Start · Stop · Restart · View dispatcher log · View app log |
| **View** | Reload · Open in browser |
| **Help** | About Cairn |

Settings tabs: **General** (execution mode, projects folder, port, autostart, log level) ·
**Workers & API keys** (add/remove workers; per type: Claude Code, Codex, Pi, or Mock for a
no-LLM demo) · **Performance** (concurrency, task timeouts, health checks) ·
**Environment** (variables for all workers, e.g. a proxy) · **Container mode** (image,
network, capabilities).

Only one copy of the app runs at a time. Closing the window stops the dispatcher and any agent
processes it started.

**Uninstall** from *Settings → Apps → Cairn*. The uninstaller asks whether to also delete
Cairn's data (database, settings, logs and saved API keys). Your projects folder is never
deleted.

**Where things are:** settings in `%LOCALAPPDATA%\cairn\desktop\settings.json`, logs in
`%LOCALAPPDATA%\cairn\logs\` (`desktop.log`, `dispatcher.log`), and the database is shared
with the command-line tools.

### Building the app and installer

```powershell
.\desktop\build.ps1             # deps → icon → tests → Cairn.exe (PyInstaller) → installer (Inno Setup)
.\desktop\build.ps1 -SkipTests
```

The build produces `build\dist\Cairn\Cairn.exe` and `dist\Cairn-Setup-<version>.exe`. The
installer step needs Inno Setup (`winget install JRSoftware.InnoSetup`). Without it, the build
stops after `Cairn.exe`.

CI builds the same installer on `windows-latest` (`.github/workflows/build-windows-installer.yml`) on every push to `main` and on `v*` tags; push a tag such as `v0.1.0` to publish a release with the installer attached.

To run the app from source: `uv run --project cairn --python 3.13 --extra desktop python -m cairn.desktop`.

Developer options (environment variables): `CAIRN_DATA_DIR` (use another data folder, e.g. a
portable or test profile), `CAIRN_KEYRING_SERVICE` (use a different Credential Manager name),
`CAIRN_WEBVIEW_DEBUG_PORT` (open the DevTools protocol on that port).

## Quick start (command line)

Open PowerShell in this folder:

```powershell
.\cairn.ps1 setup        # installs uv + a private Python 3.13 + dependencies, creates dispatch.yaml
notepad dispatch.yaml    # check workspace_root and which workers are enabled
.\cairn.ps1 start        # opens the server and the dispatcher in two windows, plus the UI
```

The UI is at http://127.0.0.1:8000/. Create a project there with an origin and a goal.
The dispatcher picks it up automatically.

From `cmd.exe`, or if your execution policy blocks `.ps1` files, use `.\cairn.cmd` instead
(`.\cairn.cmd setup`, `.\cairn.cmd start`, ...). It runs the same script with
`-ExecutionPolicy Bypass` for that process only and doesn't change any machine settings.
Keep the `.\` prefix: some locked-down machines set `NoDefaultCurrentDirectoryInExePath`, and
then cmd won't find a bare `cairn.cmd` in the current folder.

### Try it without any LLM

`dispatch.windows-mock.yaml` uses the built-in mock worker. It needs no CLI, no account and
no API key, so you can use it to check the whole pipeline on a new machine:

```powershell
.\cairn.ps1 start -Config dispatch.windows-mock.yaml
```

Create a project in the UI. Within a few seconds you'll see facts and intents appear and the
project completes.

## Prerequisites

| Need | Notes |
|---|---|
| Windows 10 or 11 | Tested with Windows PowerShell 5.1. PowerShell 7 also works. |
| Python (any version) | Only used to install `uv`. `uv` then downloads its own Python 3.13, so a too-new system Python (e.g. 3.15, which has no wheels for some dependencies yet) doesn't matter. You can also skip Python and run `winget install astral-sh.uv`. |
| A worker CLI, logged in | e.g. Claude Code: `claude -p "hello"` must work in a normal PowerShell window. Native installs (`claude.exe`) and npm installs (`codex.cmd`, `pi.cmd`) both work. |
| Node.js | Only needed for npm-installed CLIs, which already require it. |

## Launcher commands

| Command | What it does |
|---|---|
| `.\cairn.ps1 setup` | Installs `uv` if missing, syncs dependencies, copies `dispatch.windows.example.yaml` to `dispatch.yaml`. |
| `.\cairn.ps1 start [-Config file]` | Starts the server, waits for it, starts the dispatcher, opens the UI. |
| `.\cairn.ps1 serve [-BindHost h] [-Port n]` | Server only (default `127.0.0.1:8000`). |
| `.\cairn.ps1 dispatch [-Config file]` | Dispatcher only (default `dispatch.yaml`). |
| `.\cairn.ps1 check [-Config file]` | Checks that the configured CLIs are installed and runnable, then exits. |
| `.\cairn.ps1 test [-- pytest args]` | Runs the test suite, e.g. `.\cairn.ps1 test -- -k platform -q`. |

Arguments after `--` are passed to the underlying command unchanged, e.g.
`.\cairn.ps1 serve -- --db-path D:/cairn/cairn.db`. This works the same way from
PowerShell, `powershell -File` and `.\cairn.cmd`.

You can also use the upstream commands directly, e.g.
`uv run --project cairn --python 3.13 cairn serve`, or `python -m cairn ...` from inside the venv.

## Where things go

| What | Location |
|---|---|
| Database | `%LOCALAPPDATA%\cairn\cairn.db` (upstream uses `~/.local/share/cairn` on Linux). Override with `cairn serve --db-path`. |
| Project workspaces | `<workspace_root>\<project_id>\`. Set `workspace_root` in `dispatch.yaml`; the example uses `C:/cairn-runs`. |
| Graph snapshots given to agents | `<project workspace>\.cairn-prompts\...` |
| pi session files (local mode) | `%TEMP%\cairn-pi\<worker>\sessions` |

In YAML, write Windows paths with forward slashes (`C:/cairn-runs`) or doubled backslashes
(`"C:\\cairn-runs"`).

## What changed compared to upstream

The fork changes only what was POSIX-specific. Linux and macOS behaviour is the same as upstream.

| Area | Upstream (Linux) | Windows port |
|---|---|---|
| Stopping a worker (timeout/cancel) | `start_new_session` + `os.killpg` with `SIGTERM` then `SIGKILL` | New process group + `taskkill /T` (graceful), then `taskkill /T /F`. Kills the whole process tree. See `runtime/platform_compat.py`. |
| Launching CLIs | Bare name (`claude`) | Resolved through `PATHEXT`. npm `.cmd` shims are unwrapped to `node <cli>.js` so `cmd.exe` doesn't cut multi-line prompts at the first newline or mangle `& \| % ^ < > "`. |
| Worker environment | `{**os.environ, **env}` | Case-insensitive merge, so a configured `https_proxy` reliably overrides a host `HTTPS_PROXY`. |
| Graph snapshot path (local mode) | `/tmp/cairn-prompts/...` (not an absolute path on Windows, so every local task failed) | Inside the project workspace (native path, removed together with the project). |
| pi worker (local mode) | `/bin/sh -lc` wrapper, `/tmp/cairn-pi` | Runs `pi` directly; session dir created in Python under `%TEMP%`. |
| Mock worker (local mode) | `python3` (on Windows usually only a Microsoft Store stub) | The dispatcher's own interpreter. |
| Database location | `~/.local/share/cairn` | `%LOCALAPPDATA%\cairn` (XDG on Linux/macOS). |
| Project folder names | Replaces `/` only | Also replaces `\ : * ? " < > \|`. |
| CLI probe at startup | Runs bare `claude --help` | Runs the resolved path, without opening a console window. |
| Entry point | `cairn` script | Also `python -m cairn`. |
| Log output when redirected (file, pipe, service) | ANSI code page (cp1252): non-Latin text becomes `已...` escapes, and `—` becomes a byte that isn't valid UTF-8 | UTF-8. An interactive console is unchanged. |

Container mode is unchanged: those commands run inside the Linux worker image, where the
POSIX paths and `/bin/sh` are correct.

Tests: `cairn/tests/test_platform_compat.py` covers the Windows behaviour, including a real
round trip of a multi-line prompt full of shell metacharacters through an npm-style shim.
The existing local-execution tests now use Python children instead of `sh`/`sleep`, so the
whole suite passes on Windows without Git Bash.

## Container mode on Windows

1. Install Docker Desktop with the WSL 2 backend and switch it to Linux containers.
2. `docker pull --platform=linux/amd64 ghcr.io/oritera/cairn-worker-container:latest`
3. `copy dispatch.example.yaml dispatch.yaml` and fill in endpoints and keys.
4. Either:
   - run everything in Docker: `docker compose up --build`. Docker Desktop maps the
     `/var/run/docker.sock` mount in `docker-compose.yaml` to its engine. Or:
   - run the dispatcher natively: `.\cairn.ps1 start`. The Docker SDK connects through
     Docker Desktop's named pipe (`npipe:////./pipe/docker_engine`) automatically.

`network_mode: host` behaves differently under Docker Desktop than on a Linux host: the
container gets the network of the Docker VM, not of your PC. If workers need to reach
services on your PC, use `host.docker.internal`.

## Troubleshooting

- **`uv not found`**: run `.\cairn.ps1 setup`, or `winget install astral-sh.uv`.
- **`Microsoft Visual C++ 14.0 is required`** when installing with plain `pip`: your system
  Python is too new for the prebuilt wheels. Use the launcher (it pins Python 3.13), or
  `uv sync --project cairn --python 3.13`.
- **`[-] claude not found on PATH`**: open a *new* PowerShell window after installing the CLI
  and check that `where.exe claude` finds it. The dispatcher inherits PATH from the window
  you start it in.
- **Red `NativeCommandError` text in Windows PowerShell 5.1**: this is how 5.1 shows
  anything a program writes to stderr. Cairn logs to stderr, so these lines are normal log
  output, not errors.
- **Slow first `setup`**: `cairn/pyproject.toml` (inherited from upstream) uses the Aliyun
  PyPI mirror. It works worldwide but may be slower outside China.

## Safety

Local mode runs AI agents with **your Windows user's permissions and no sandbox**. They can
read and write files and run commands. Give them a dedicated `workspace_root`, and only use
Cairn on systems and targets you are authorized to work on. See the
[Disclaimer](README-UPSTREAM.md#disclaimer).
