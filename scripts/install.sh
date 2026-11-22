#!/usr/bin/env sh
# Clipper installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/k-a-l/clipper/main/scripts/install.sh | sh
#
# What it does (each step is skipped if already done):
#   1. installs FFmpeg and Node.js (Homebrew on macOS; apt / dnf / pacman / zypper on Linux)
#   2. installs uv (https://docs.astral.sh/uv/) — a Python tool manager; it fetches Python 3.12 itself
#   3. installs the `clipper` command from GitHub (or from this folder, if run inside a checkout)
#   4. creates ~/Clipper/.env with a fresh encryption key
#
# Environment overrides: CLIPPER_REPO (default k-a-l/clipper), CLIPPER_REF (default main),
# CLIPPER_HOME (default ~/Clipper), CLIPPER_SKIP_SYSTEM=1 (don't install ffmpeg/node).
set -eu

REPO="${CLIPPER_REPO:-k-a-l/clipper}"
REF="${CLIPPER_REF:-main}"
HOME_DIR="${CLIPPER_HOME:-$HOME/Clipper}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

sudo_cmd() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

install_system_deps() {
  if have ffmpeg && have ffprobe && have node; then
    say "FFmpeg and Node.js already installed"
    return
  fi
  case "$(uname -s)" in
    Darwin)
      if ! have brew; then
        warn "Homebrew not found. Install it from https://brew.sh, then run this script again."
        exit 1
      fi
      say "Installing FFmpeg and Node.js with Homebrew"
      brew install ffmpeg node
      ;;
    Linux)
      if have apt-get; then
        say "Installing FFmpeg and Node.js with apt (needs sudo)"
        sudo_cmd apt-get update
        sudo_cmd apt-get install -y ffmpeg nodejs fonts-dejavu-core ca-certificates curl
      elif have dnf; then
        say "Installing FFmpeg and Node.js with dnf (needs sudo; FFmpeg comes from RPM Fusion on Fedora)"
        sudo_cmd dnf install -y ffmpeg nodejs dejavu-sans-fonts || {
          warn "dnf couldn't install ffmpeg. Enable RPM Fusion (https://rpmfusion.org/Configuration) and re-run."
          exit 1
        }
      elif have pacman; then
        say "Installing FFmpeg and Node.js with pacman (needs sudo)"
        sudo_cmd pacman -Sy --needed --noconfirm ffmpeg nodejs ttf-dejavu
      elif have zypper; then
        say "Installing FFmpeg and Node.js with zypper (needs sudo)"
        sudo_cmd zypper --non-interactive install ffmpeg nodejs dejavu-fonts
      else
        warn "Unknown package manager: install ffmpeg and nodejs yourself, then re-run with CLIPPER_SKIP_SYSTEM=1."
        exit 1
      fi
      ;;
    *)
      warn "Unsupported system $(uname -s). On Windows use scripts/install.ps1."
      exit 1
      ;;
  esac
}

install_uv() {
  if have uv; then
    say "uv already installed"
    return
  fi
  say "Installing uv (Python tool manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # the installer puts uv in ~/.local/bin (or $XDG_BIN_HOME); make it usable in this shell
  PATH="$HOME/.local/bin:${XDG_BIN_HOME:-$HOME/.local/bin}:$PATH"
  export PATH
  have uv || { warn "uv was installed but isn't on PATH; open a new terminal and re-run."; exit 1; }
}

install_clipper() {
  script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || echo "")
  if [ -n "$script_dir" ] && [ -f "$script_dir/../pyproject.toml" ]; then
    src="$script_dir/.."
    say "Installing clipper from this folder"
  else
    src="git+https://github.com/$REPO@$REF"
    say "Installing clipper from github.com/$REPO ($REF)"
  fi
  uv tool install --force --python 3.12 "$src"
  uv tool update-shell >/dev/null 2>&1 || true
}

setup_home() {
  mkdir -p "$HOME_DIR"
  if [ -f "$HOME_DIR/.env" ]; then
    say "Keeping your existing settings in $HOME_DIR/.env"
    return
  fi
  key=$(clipper gen-key 2>/dev/null | tail -n 1 || true)
  {
    echo "# Clipper settings — see docs/CREDENTIALS.md for how to fill these in."
    echo "TOKEN_ENCRYPTION_KEY=$key"
    echo "YOUTUBE_CLIENT_ID="
    echo "YOUTUBE_CLIENT_SECRET="
    echo "YOUTUBE_PRIVACY_STATUS=private"
    echo "FACEBOOK_APP_ID="
    echo "FACEBOOK_APP_SECRET="
    echo "FACEBOOK_PAGE_ID="
    echo "FACEBOOK_VIDEO_STATE=DRAFT"
  } > "$HOME_DIR/.env"
  chmod 600 "$HOME_DIR/.env"
  say "Created $HOME_DIR/.env (with a new encryption key)"
}

[ "${CLIPPER_SKIP_SYSTEM:-0}" = "1" ] || install_system_deps
install_uv
install_clipper
PATH="$HOME/.local/bin:$PATH"; export PATH
setup_home

say "Done! Start the dashboard with:"
echo
echo "    clipper ui --open"
echo
echo "    (open a new terminal first if 'clipper' isn't found)"
echo "    Connect YouTube / Facebook: see docs/CREDENTIALS.md — https://github.com/$REPO/blob/$REF/docs/CREDENTIALS.md"
