# Shared helpers for the macOS scripts (setup.command, update.command, Diagnose.command,
# install-codex.sh, mac-app.sh, Start Claude Overlay.command). Sourced, never run:
#     OV_REPO="<the app folder>"; . "$OV_REPO/mac-common.sh"
#
# The Windows scripts each carry their own copy of "find a Python that actually runs",
# because cmd has no way to share one. bash does, so the macOS scripts share this file.

OV_STATE="$HOME/Library/Application Support/claude-overlay"   # config.py's _data_home()
OV_VENV="$OV_REPO/.venv"
OV_PY="$OV_VENV/bin/python"
OV_UV="$OV_STATE/tools/uv"
OV_UV_PYTHONS="$OV_STATE/python"
OV_PY_VERSION="3.12"

# The folders `claude`, `codex`, `node` and `npm` are usually installed into. A script
# double-clicked in Finder gets the login shell's PATH from Terminal, but one the overlay
# starts for itself (the Update / Set up Codex buttons) inherits the app's, so add them
# here rather than assume. ~/.local/node is where install-codex.sh puts Node.js.
ov_path() {
  # OV_TEST_PATH: the test suite's hook - use exactly this PATH, so a test decides which
  # CLIs "exist" instead of inheriting whatever the machine running it has installed.
  if [ -n "$OV_TEST_PATH" ]; then PATH="$OV_TEST_PATH"; export PATH; return; fi
  PATH="$HOME/.local/bin:$HOME/.local/node/bin:/opt/homebrew/bin:/usr/local/bin:$HOME/.npm-global/bin:$PATH"
  export PATH
}

ov_rule() { echo "============================================================"; }

# `pause`: wait for Return, but only when somebody is there to press it — under the test
# suite or a pipe there is no terminal, and waiting would hang forever.
ov_pause() {
  if [ -t 0 ]; then
    printf '%s' "${1:-Press Return to close this window... }"
    read -r _ || true
  fi
}

# Ask a yes/no question with Yes as the default. Returns 0 for yes. With no terminal to
# ask on, the answer is the default the caller passes as $2 (0 = yes, 1 = no).
ov_ask() {
  if [ ! -t 0 ]; then return "${2:-0}"; fi
  printf '%s [Y/n] ' "$1"
  read -r ans || ans=""
  case "$ans" in n*|N*) return 1 ;; *) return 0 ;; esac
}

# Does this interpreter have what the overlay needs: Python 3.10+ and a Tk it can draw
# with? The Python that ships with macOS (/usr/bin/python3) fails both — it is 3.9, built
# against the Tk 8.5 Apple deprecated — and Homebrew's often has no tkinter at all, so
# "python3 exists" is not the test. Nor is "tkinter imports": tkinter.Tcl() is what finds
# out that the Tcl library beside it can't be loaded (some relocated builds, once inside
# a virtual environment), which would otherwise only show when the app tries to open.
ov_python_ok() {
  [ -x "$1" ] || return 1
  "$1" -c 'import sys, tkinter
tkinter.Tcl()
raise SystemExit(0 if sys.version_info >= (3, 10) and tkinter.TkVersion >= 8.6 else 1)' \
    >/dev/null 2>&1
}

# Print the first usable Python on this Mac, or nothing.
ov_find_base_python() {
  local c p
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    if ov_python_ok "$p"; then echo "$p"; return 0; fi
  done
  for p in /Library/Frameworks/Python.framework/Versions/3.*/bin/python3 \
           /opt/homebrew/bin/python3.1? /usr/local/bin/python3.1?; do
    if ov_python_ok "$p"; then echo "$p"; return 0; fi
  done
  if [ -x "$OV_UV" ]; then
    p="$(UV_PYTHON_INSTALL_DIR="$OV_UV_PYTHONS" "$OV_UV" python find --managed-python \
         "$OV_PY_VERSION" 2>/dev/null)"
    if ov_python_ok "$p"; then echo "$p"; return 0; fi
  fi
  return 1
}

# Install a private Python with uv, into the overlay's own data folder: no admin
# password, no installer to click through, nothing touched system-wide. uv is fetched as
# a release archive straight from its GitHub project and checked against the checksum
# published beside it before anything in it is run.
ov_install_python() {
  local arch tmp url want got
  case "$(uname -m)" in
    arm64) arch="aarch64" ;;
    x86_64) arch="x86_64" ;;
    *) echo "[X] Unsupported Mac architecture: $(uname -m)"; return 1 ;;
  esac
  url="https://github.com/astral-sh/uv/releases/latest/download/uv-$arch-apple-darwin.tar.gz"
  tmp="$(mktemp -d)" || return 1
  echo "    Downloading uv (the tool that installs Python) ..."
  if ! curl -fL --progress-bar -o "$tmp/uv.tar.gz" "$url" \
     || ! curl -fsSL -o "$tmp/uv.sha256" "$url.sha256"; then
    echo "[X] The download was refused or failed (see above)."
    rm -rf "$tmp"; return 1
  fi
  want="$(cut -d' ' -f1 "$tmp/uv.sha256")"
  got="$(shasum -a 256 "$tmp/uv.tar.gz" | cut -d' ' -f1)"
  if [ -z "$want" ] || [ "$want" != "$got" ]; then
    echo "[X] The download doesn't match its published checksum - not using it."
    rm -rf "$tmp"; return 1
  fi
  mkdir -p "$(dirname "$OV_UV")" && tar -xzf "$tmp/uv.tar.gz" -C "$tmp" \
    && cp "$tmp/uv-$arch-apple-darwin/uv" "$OV_UV"
  local rc=$?
  rm -rf "$tmp"
  [ $rc -eq 0 ] || { echo "[X] Could not unpack uv."; return 1; }
  echo "    Installing Python $OV_PY_VERSION ..."
  # Verify against the keychain, so a network that inspects HTTPS (and re-signs it with
  # the organisation's own root) doesn't fail the download. Two names for one switch: uv
  # renamed UV_NATIVE_TLS to UV_SYSTEM_CERTS, and each release reads the one it knows.
  # --no-bin: don't link a python3.x into ~/.local/bin - this Python is the overlay's own.
  UV_NATIVE_TLS=1 UV_SYSTEM_CERTS=1 UV_PYTHON_INSTALL_DIR="$OV_UV_PYTHONS" \
    "$OV_UV" python install --no-bin "$OV_PY_VERSION" || return 1
}

# Make sure the app folder has a working virtual environment, creating it from $1 (a
# base interpreter) when it doesn't. The environment is the one thing every macOS
# launcher runs, so "which Python is the app using" always has one answer.
ov_ensure_venv() {
  if ov_python_ok "$OV_PY"; then return 0; fi
  [ -n "$1" ] || return 1
  if [ -e "$OV_VENV" ]; then
    echo "    The existing environment in .venv doesn't work - rebuilding it."
    "$1" -m venv --clear "$OV_VENV" || return 1
  else
    "$1" -m venv "$OV_VENV" || return 1
  fi
  ov_python_ok "$OV_PY"
}

# Install from the requirements files into the environment. $1 = "required" stops on a
# failure of requirements.txt; the docs extras are optional either way.
ov_pip_install() {
  if ! "$OV_PY" -m pip --version >/dev/null 2>&1; then
    echo "pip is missing from this environment - bootstrapping it with ensurepip ..."
    "$OV_PY" -m ensurepip --upgrade || return 1
  fi
  "$OV_PY" -m pip install --upgrade -r "$OV_REPO/requirements.txt" || return 1
  if [ -f "$OV_REPO/requirements-docs.txt" ]; then
    echo "Installing optional Word/PowerPoint support ..."
    if ! "$OV_PY" -m pip install --upgrade -r "$OV_REPO/requirements-docs.txt"; then
      echo
      echo "[NOTE] Optional Word/PowerPoint support did not install (see above)."
      echo "    Everything else is fine and the app will start normally. Attaching a"
      echo "    .docx or .pptx will say support is missing; images and PDFs are"
      echo "    unaffected. To retry just this part:"
      echo "      \"$OV_PY\" -m pip install --upgrade -r requirements-docs.txt"
    fi
  fi
  return 0
}
