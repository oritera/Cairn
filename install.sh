#!/usr/bin/env bash
# Installer for our modified Cairn on a fresh machine.
#
#   curl -fsSL <raw-url>/install.sh | bash        # bootstrap: clone + set up
#   ./install.sh                                   # run from inside an existing clone
#
# What it does (idempotent — safe to re-run):
#   1. checks prerequisites (git, a container engine, uv — installs uv if missing)
#   2. clones the repo at the right branch (only when not already inside a clone)
#   3. installs the Python package + deps with `uv sync`
#   4. pulls the worker container image
#   5. seeds dispatch.yaml from the example if absent (you then add your API keys)
#   6. smoke-tests the CLI and prints next steps
#
# Override any of these with env vars:
#   REPO_URL   (default: https://github.com/bronezppg-arch/Cairn.git)
#   BRANCH     (default: feat/ops-dashboard-cost-supervisor)
#   INSTALL_DIR(default: $HOME/Cairn   — only used when bootstrapping a fresh clone)
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/bronezppg-arch/Cairn.git}"
BRANCH="${BRANCH:-feat/ops-dashboard-cost-supervisor}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/Cairn}"
WORKER_IMAGE="ghcr.io/oritera/cairn-worker-container:latest"

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# 1. prerequisites
# ---------------------------------------------------------------------------
say "Checking prerequisites"

have git || die "git is required — install it first (e.g. 'sudo apt-get install -y git')"
echo "  git      : $(git --version | awk '{print $3}')"

# container engine: docker preferred, podman accepted
CONTAINER_ENGINE=""
if have docker; then CONTAINER_ENGINE="docker"
elif have podman; then CONTAINER_ENGINE="podman"
fi
if [ -z "$CONTAINER_ENGINE" ]; then
  warn "no container engine found. Workers run in containers, so install one, e.g.:"
  warn "    sudo apt-get install -y docker.io && sudo usermod -aG docker \"\$USER\"   (re-login after)"
  warn "continuing setup, but the dispatcher won't be able to run workers until this is fixed."
else
  echo "  container: $CONTAINER_ENGINE ($($CONTAINER_ENGINE --version 2>/dev/null | head -1))"
  if ! $CONTAINER_ENGINE ps >/dev/null 2>&1; then
    warn "$CONTAINER_ENGINE is installed but not usable by this user."
    warn "start the daemon and/or add yourself to the group:  sudo usermod -aG docker \"\$USER\"  (re-login)"
  fi
fi

# uv: install if missing
if ! have uv; then
  say "uv not found — installing it"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
have uv || die "uv still not on PATH — add \$HOME/.local/bin to PATH and re-run"
echo "  uv       : $(uv --version | awk '{print $2}')"

# ---------------------------------------------------------------------------
# 2. obtain the source (skip when already inside a clone)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
if [ -f "$SCRIPT_DIR/cairn/pyproject.toml" ]; then
  REPO_DIR="$SCRIPT_DIR"
  say "Using existing clone at $REPO_DIR"
  git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null | grep -q . \
    && echo "  branch   : $(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)"
else
  say "Cloning $REPO_URL ($BRANCH) -> $INSTALL_DIR"
  if [ -d "$INSTALL_DIR/.git" ]; then
    echo "  already cloned — fetching latest"
    git -C "$INSTALL_DIR" fetch origin "$BRANCH"
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH" || warn "could not fast-forward; leaving as-is"
  else
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
  REPO_DIR="$INSTALL_DIR"
fi
cd "$REPO_DIR"

# ---------------------------------------------------------------------------
# 3. install the package + dependencies
# ---------------------------------------------------------------------------
say "Installing Python deps (uv sync)"
uv sync --project cairn

# ---------------------------------------------------------------------------
# 4. pull the worker container image
# ---------------------------------------------------------------------------
if [ -n "$CONTAINER_ENGINE" ] && $CONTAINER_ENGINE ps >/dev/null 2>&1; then
  say "Pulling worker image $WORKER_IMAGE"
  if ! $CONTAINER_ENGINE pull "$WORKER_IMAGE"; then
    warn "pull failed (private registry or no network?). If it needs auth:"
    warn "    echo \$GHCR_TOKEN | $CONTAINER_ENGINE login ghcr.io -u <user> --password-stdin"
    warn "then: $CONTAINER_ENGINE pull $WORKER_IMAGE"
  fi
else
  warn "skipping image pull — no usable container engine yet"
fi

# ---------------------------------------------------------------------------
# 5. seed a config
# ---------------------------------------------------------------------------
if [ ! -f dispatch.yaml ]; then
  if [ -f dispatch.example.yaml ]; then
    cp dispatch.example.yaml dispatch.yaml
    say "Seeded dispatch.yaml from dispatch.example.yaml"
    warn "EDIT dispatch.yaml and add your model/base_url/API key before starting."
    warn "(config files are gitignored — your keys never get committed)"
  else
    warn "no dispatch.example.yaml found; create dispatch.yaml yourself or use the dashboard wizard."
  fi
else
  echo "  dispatch.yaml already present — leaving it untouched"
fi

# ---------------------------------------------------------------------------
# 6. smoke test + next steps
# ---------------------------------------------------------------------------
say "Verifying the CLI"
uv run --project cairn cairn --help >/dev/null && echo "  cairn CLI OK"

chmod +x start-cairn.sh 2>/dev/null || true

cat <<DONE

$(printf '\033[1;32m✓ Cairn is installed.\033[0m')  Repo: $REPO_DIR

Next:
  1. cd $REPO_DIR
  2. edit dispatch.yaml  — set your worker model, base_url and API key
     (or start the server and build it in the dashboard's Конфигуратор)
  3. ./start-cairn.sh                 # starts server + supervisor (owns the dispatcher)
     ./start-cairn.sh dispatch.yaml.X # use a named config instead
     ./start-cairn.sh stop            # stop everything
  4. open http://127.0.0.1:8000  → pick a project → Navigation tab

Data (projects/facts) lives in ~/.local/share/cairn/ — a fresh machine starts empty.
DONE
