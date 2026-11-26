#!/bin/sh
# CamXposed MCP - One-Command Self-Installing Launcher for Linux & Android Termux
# Usage:
#   Inside repo:  ./start.sh [args...]
#   One-line run: curl -fsSL https://raw.githubusercontent.com/SubhranshuSharma/CamXposed-MCP/feature/live-voice-bridge-and-visual-subagent/start.sh | bash -s -- [args...]
set -eu

REPO_URL="https://github.com/SubhranshuSharma/CamXposed-MCP.git"
TARGET_BRANCH="${CAMXPOSED_BRANCH:-feature/live-voice-bridge-and-visual-subagent}"
INSTALL_DIR="${CAMXPOSED_INSTALL_DIR:-$HOME/camxposed-mcp}"

say() { printf '%s\n' "$*"; }
fail() { say "Error: $*" >&2; exit 1; }

# 1. Detect environment
is_termux=0
case "${PREFIX:-}" in *com.termux*) is_termux=1 ;; esac
if [ -n "${TERMUX_VERSION:-}" ]; then is_termux=1; fi

# 2. Check and install system prerequisites if needed
install_prereqs() {
  if [ "$is_termux" -eq 1 ]; then
    command -v pkg >/dev/null 2>&1 || fail "Termux package manager (pkg) was not found."
    missing=""
    command -v git >/dev/null 2>&1 || missing="$missing git"
    command -v python >/dev/null 2>&1 || missing="$missing python"
    command -v node >/dev/null 2>&1 || missing="$missing nodejs"
    command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"
    if [ -n "$missing" ]; then
      say "Installing Termux prerequisites:$missing..."
      pkg update -y
      pkg install -y git python nodejs ffmpeg clang make libjpeg-turbo
      pkg install -y python-numpy 2>/dev/null || true
    fi
  elif command -v apt-get >/dev/null 2>&1; then
    missing=""
    command -v git >/dev/null 2>&1 || missing="$missing git"
    command -v python3 >/dev/null 2>&1 || missing="$missing python3 python3-pip"
    command -v node >/dev/null 2>&1 || missing="$missing nodejs npm"
    command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"
    if [ -n "$missing" ]; then
      say "Installing Linux (apt) prerequisites:$missing..."
      if [ "$(id -u)" -eq 0 ]; then SUDO=""; else
        command -v sudo >/dev/null 2>&1 || fail "Please install sudo or run as root."
        SUDO="sudo"
      fi
      $SUDO apt-get update
      $SUDO apt-get install -y git python3 python3-pip ffmpeg nodejs npm
    fi
  elif command -v dnf >/dev/null 2>&1; then
    missing=""
    command -v git >/dev/null 2>&1 || missing="$missing git"
    command -v python3 >/dev/null 2>&1 || missing="$missing python3 python3-pip"
    command -v node >/dev/null 2>&1 || missing="$missing nodejs npm"
    command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"
    if [ -n "$missing" ]; then
      say "Installing Linux (dnf) prerequisites:$missing..."
      if [ "$(id -u)" -eq 0 ]; then SUDO=""; else
        command -v sudo >/dev/null 2>&1 || fail "Please install sudo or run as root."
        SUDO="sudo"
      fi
      $SUDO dnf install -y git python3 python3-pip ffmpeg nodejs npm
    fi
  elif command -v pacman >/dev/null 2>&1; then
    missing=""
    command -v git >/dev/null 2>&1 || missing="$missing git"
    command -v python >/dev/null 2>&1 || missing="$missing python python-pip"
    command -v node >/dev/null 2>&1 || missing="$missing nodejs npm"
    command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"
    if [ -n "$missing" ]; then
      say "Installing Linux (pacman) prerequisites:$missing..."
      if [ "$(id -u)" -eq 0 ]; then SUDO=""; else
        command -v sudo >/dev/null 2>&1 || fail "Please install sudo or run as root."
        SUDO="sudo"
      fi
      $SUDO pacman -Sy --needed --noconfirm git python python-pip ffmpeg nodejs npm
    fi
  fi
}

install_prereqs

# 3. Locate or clone repository
SCRIPT_DIR=""
if [ -f "run.py" ] && [ -f "server.py" ]; then
  SCRIPT_DIR="$(pwd)"
elif [ -e "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/run.py" ]; then
  SCRIPT_DIR="$INSTALL_DIR"
  # Pull latest changes if working directory is clean
  if [ -z "$(git -C "$INSTALL_DIR" status --porcelain 2>/dev/null || true)" ]; then
    say "Updating checkout to origin/$TARGET_BRANCH..."
    git -C "$INSTALL_DIR" fetch origin "$TARGET_BRANCH" 2>/dev/null || true
    git -C "$INSTALL_DIR" checkout "$TARGET_BRANCH" 2>/dev/null || true
    git -C "$INSTALL_DIR" pull --ff-only origin "$TARGET_BRANCH" 2>/dev/null || true
  fi
else
  say "Cloning CamXposed MCP ($TARGET_BRANCH) into $INSTALL_DIR..."
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --depth 1 -b "$TARGET_BRANCH" "$REPO_URL" "$INSTALL_DIR"
  SCRIPT_DIR="$INSTALL_DIR"
fi

cd "$SCRIPT_DIR"

# 4. Launch run.py (which auto-verifies Python dependencies, ngrok Node SDK, and integrity checks)
say "Starting CamXposed MCP on $TARGET_BRANCH..."
if [ "$is_termux" -eq 1 ]; then
  exec python run.py "$@"
else
  exec python3 run.py "$@"
fi
