#!/bin/sh
# One-time bootstrap for CamXposed MCP. Safe to run as: curl -fsSL URL | sh
set -eu

INSTALL_DIR="${CAMXPOSED_INSTALL_DIR:-$HOME/camxposed-mcp}"

say() { printf '%s\n' "$*"; }
fail() { say "Error: $*" >&2; exit 1; }

if [ -z "${HOME:-}" ]; then
  fail "HOME is not set."
fi

is_termux=0
case "${PREFIX:-}" in *com.termux*) is_termux=1 ;; esac
if [ -n "${TERMUX_VERSION:-}" ]; then is_termux=1; fi

if [ "$is_termux" -eq 1 ]; then
  command -v pkg >/dev/null 2>&1 || fail "Termux package manager (pkg) was not found."
  say "Installing Termux prerequisites..."
  pkg update
  pkg install -y git python nodejs ffmpeg
elif command -v apt-get >/dev/null 2>&1; then
  say "Installing Debian/Ubuntu prerequisites..."
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; else
    command -v sudo >/dev/null 2>&1 || fail "Please install sudo or run this installer as root."
    SUDO="sudo"
  fi
  $SUDO apt-get update
  $SUDO apt-get install -y git python3 python3-pip ffmpeg nodejs npm
elif command -v dnf >/dev/null 2>&1; then
  say "Installing Fedora/RHEL prerequisites..."
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; else
    command -v sudo >/dev/null 2>&1 || fail "Please install sudo or run this installer as root."
    SUDO="sudo"
  fi
  $SUDO dnf install -y git python3 python3-pip ffmpeg nodejs npm
elif command -v pacman >/dev/null 2>&1; then
  say "Installing Arch Linux prerequisites..."
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; else
    command -v sudo >/dev/null 2>&1 || fail "Please install sudo or run this installer as root."
    SUDO="sudo"
  fi
  $SUDO pacman -Sy --needed --noconfirm git python python-pip ffmpeg nodejs npm
else
  fail "Unsupported system. Supported: Termux, Debian/Ubuntu, Fedora/RHEL, and Arch Linux."
fi

if [ -e "$INSTALL_DIR" ]; then
  [ -d "$INSTALL_DIR/.git" ] || fail "$INSTALL_DIR exists but is not a Git repository. Set CAMXPOSED_INSTALL_DIR to another path."
  origin="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
  case "$origin" in
    https://github.com/SubhranshuSharma/CamXposed-MCP.git|git@github.com:SubhranshuSharma/CamXposed-MCP.git) ;;
    *) fail "$INSTALL_DIR is a Git repository, but its origin is not CamXposed-MCP: $origin" ;;
  esac
  [ -z "$(git -C "$INSTALL_DIR" status --porcelain)" ] || fail "$INSTALL_DIR has local changes. Commit/stash them or choose another CAMXPOSED_INSTALL_DIR."
  say "Updating existing CamXposed MCP checkout..."
  branch="$(git -C "$INSTALL_DIR" branch --show-current)"
  if [ "$branch" != "main" ]; then
    git -C "$INSTALL_DIR" checkout main
  fi
  git -C "$INSTALL_DIR" pull --ff-only origin main
else
  say "Cloning CamXposed MCP into $INSTALL_DIR ..."
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --depth 1 https://github.com/SubhranshuSharma/CamXposed-MCP.git "$INSTALL_DIR"
fi

say ""
say "Setup complete."
say "Start CamXposed MCP with:"
if [ "$is_termux" -eq 1 ]; then
  say "  cd \"$INSTALL_DIR\" && python run.py"
else
  say "  cd \"$INSTALL_DIR\" && python3 run.py"
fi
say ""
say "The first launch will ask for your ngrok token once and save it securely."
