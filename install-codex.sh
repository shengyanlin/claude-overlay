#!/bin/bash
# Set up Codex mode (macOS): install the `codex` CLI and sign it in.
#
# The macOS counterpart of install-codex.cmd - same modes, same exit codes, same two
# marker files, because the overlay reads all three:
#
#   install-codex.sh                 run by hand: install, then offer to sign in.
#   install-codex.sh --from-setup    from setup.command.
#   install-codex.sh --from-update   from update.command: ASKS before installing, and a
#                                    "no" is remembered so later updates don't ask again.
#   install-codex.sh --from-app      the overlay's "Set up Codex" button: installs and
#                                    signs in without asking (the click was the answer).
#
#   exit 0  installed and signed in      exit 2  installed, not signed in
#   exit 3  not installed
#
#   <state>/codex-setup-declined     the user said no during an update.
#   <state>/codex-setup-result       "<mode> <exit code> <what failed>", left when a setup
#                                    that was TRIED didn't finish; the overlay says so at
#                                    its next launch.

OV_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$OV_REPO/mac-common.sh"
ov_path

MODE="$1"
DECLINED="$OV_STATE/codex-setup-declined"
RESULT="$OV_STATE/codex-setup-result"
NODE_HOME="$HOME/.local/node"          # where Node.js goes when this script installs it
WHY=""
RC=3

remember() { mkdir -p "$OV_STATE" 2>/dev/null; echo declined > "$DECLINED"; }

finish() {
  [ "$RC" = "0" ] && rm -f "$RESULT" 2>/dev/null
  if [ -n "$WHY" ]; then
    case "$MODE" in
      --from-update|--from-setup)
        mkdir -p "$OV_STATE" 2>/dev/null
        echo "$MODE $RC $WHY" > "$RESULT" ;;
    esac
    if [ "$MODE" = "--from-update" ] && [ -n "$OV_UPDATE_AUTO" ]; then
      # An automatic update's window closes by itself on success - so a Codex setup that
      # failed inside it has to hold the window, or the reason scrolls away unread.
      echo
      echo "[NOTE] Codex setup didn't finish -- what went wrong is above. The update itself is done:"
      echo "       press Return and the overlay restarts, where clicking Codex offers to try again."
      ov_pause "Press Return to continue... "
    fi
  fi
  if [ "$MODE" = "--from-app" ] && [ "$RC" != "0" ]; then ov_pause; fi
  [ -z "$MODE" ] && ov_pause
  exit "$RC"
}

manual() {
  echo "[NOTE] Codex CLI not installed. Claude mode works without it. For Codex mode later,"
  echo "       click Codex in the overlay's titlebar (it offers to set it up), or run:"
  echo "         npm install -g @openai/codex    (needs Node.js 18+ from https://nodejs.org)"
  echo "         codex login"
  RC=3; finish
}

not_signed() {
  echo "[NOTE] The codex CLI is installed but not signed in. Before you switch the overlay to"
  echo "       Codex, run   codex login   once -- or click Codex in the titlebar, which offers it."
  RC=2; finish
}

# Node.js LTS into the user's own folder: the official build from nodejs.org, checked
# against the checksum list published with it. No admin password, nothing system-wide.
install_node() {
  local arch tmp base file want got
  case "$(uname -m)" in
    arm64) arch="arm64" ;; x86_64) arch="x64" ;; *) return 1 ;;
  esac
  base="https://nodejs.org/dist/latest-v22.x"
  tmp="$(mktemp -d)" || return 1
  curl -fsSL -o "$tmp/SHASUMS256.txt" "$base/SHASUMS256.txt" || { rm -rf "$tmp"; return 1; }
  file="$(awk '{print $2}' "$tmp/SHASUMS256.txt" | grep -E "^node-v[0-9.]+-darwin-$arch\.tar\.gz$" | head -1)"
  [ -n "$file" ] || { rm -rf "$tmp"; return 1; }
  echo "    Downloading $file ..."
  curl -fL --progress-bar -o "$tmp/$file" "$base/$file" || { rm -rf "$tmp"; return 1; }
  want="$(awk -v f="$file" '$2 == f {print $1}' "$tmp/SHASUMS256.txt")"
  got="$(shasum -a 256 "$tmp/$file" | cut -d' ' -f1)"
  if [ -z "$want" ] || [ "$want" != "$got" ]; then
    echo "    The download doesn't match its published checksum - not using it."
    rm -rf "$tmp"; return 1
  fi
  mkdir -p "$NODE_HOME" && tar -xzf "$tmp/$file" -C "$NODE_HOME" --strip-components=1
  local rc=$?
  rm -rf "$tmp"
  return $rc
}

echo
if command -v codex >/dev/null 2>&1; then
  echo "[OK] codex CLI found."
else
  if [ "$MODE" = "--from-update" ]; then
    if [ -e "$DECLINED" ]; then
      echo "[NOTE] Codex mode isn't set up (you skipped it before). To set it up, click Codex in"
      echo "       the overlay's titlebar, or run install-codex.sh in the app folder."
      RC=3; finish
    fi
    echo "[NOTE] New in this version: Codex mode -- switch the overlay between Claude and"
    echo "       OpenAI's Codex from its titlebar. It needs the codex CLI, which isn't on"
    echo "       this Mac yet. Claude mode works the same either way."
    if ! ov_ask "Install the codex CLI now (npm install -g @openai/codex)?" 1; then
      remember; manual
    fi
  fi
  echo "[!] 'codex' CLI not found -- installing it for Codex mode."
  if ! command -v npm >/dev/null 2>&1; then
    echo "    Installing it needs npm, which comes with Node.js -- not on this Mac either."
    if ! ov_ask "Install Node.js LTS now (into your user folder, no admin needed)?" 1; then
      [ "$MODE" = "--from-update" ] && remember
      manual
    fi
    WHY="node-failed"
    if ! install_node || ! command -v npm >/dev/null 2>&1; then
      if command -v brew >/dev/null 2>&1; then
        echo "    Trying Homebrew instead ..."
        brew install node
      fi
    fi
    command -v npm >/dev/null 2>&1 || manual
  fi
  WHY="npm-failed"
  # Certificates for npm, on a network that inspects HTTPS: a current Node.js reads the
  # keychain itself (NODE_USE_SYSTEM_CA); an older one gets a PEM file of the roots this Mac
  # trusts for HTTPS. The overlay's own cliupdate writes it (one implementation, so one
  # trust check: being in a keychain isn't being trusted). No Python, or nothing trusted:
  # no file, and npm runs on NODE_USE_SYSTEM_CA alone.
  : "${NODE_USE_SYSTEM_CA:=1}"; export NODE_USE_SYSTEM_CA
  if [ -z "$NODE_EXTRA_CA_CERTS" ]; then
    PYCA="${OV_PYTHON:-$OV_PY}"
    if [ -x "$PYCA" ]; then
      PEM="$(cd "$OV_REPO" && "$PYCA" -c 'import cliupdate; print(cliupdate._mac_ca_pem() or "")' 2>/dev/null)"
      [ -n "$PEM" ] && [ -f "$PEM" ] && export NODE_EXTRA_CA_CERTS="$PEM"
    fi
  fi
  echo "    Installing it with npm  (npm install -g @openai/codex) ..."
  npm install -g @openai/codex --loglevel notice
  NPMRC=$?
  hash -r 2>/dev/null
  if ! command -v codex >/dev/null 2>&1; then
    # npm's global folder may be one this script (and the overlay) doesn't search, or one
    # the user can't write to (a system-wide Node.js). Try again into the user's own.
    NPMPREFIX="$(npm prefix -g 2>/dev/null)"
    if [ "$NPMPREFIX" != "$NODE_HOME" ]; then
      [ -n "$NPMPREFIX" ] && echo "    npm's global folder is \"$NPMPREFIX\", which isn't where codex is looked for (or isn't writable)."
      echo "    Trying again into \"$NODE_HOME\" ..."
      mkdir -p "$NODE_HOME"
      npm install -g @openai/codex --loglevel notice --prefix "$NODE_HOME"
      NPMRC=$?
      hash -r 2>/dev/null
    fi
  fi
  if ! command -v codex >/dev/null 2>&1; then
    NPMAT="$(command -v npm 2>/dev/null || echo "not found")"
    NODEVER="$(node --version 2>/dev/null || echo "not on PATH")"
    echo "[!] npm couldn't install the codex CLI (npm exit code $NPMRC) -- npm's messages above"
    echo "    say why. npm: \"$NPMAT\"   Node.js: $NODEVER"
    echo "    A certificate error (UNABLE_TO_GET_ISSUER_CERT_LOCALLY and the like) even with"
    echo "    the keychain's certificates: ask IT for this network's root certificate."
    manual
  fi
  WHY=""
  echo "[OK] codex CLI installed."
fi

# ── sign in ──
if codex login status >/dev/null 2>&1; then
  WHY=""
  echo "[OK] codex is signed in -- click Codex in the overlay's titlebar to use it."
  rm -f "$DECLINED" 2>/dev/null
  RC=0; finish
fi
if [ "$MODE" != "--from-app" ]; then
  if [ "$MODE" = "--from-update" ] && [ -e "$DECLINED" ]; then
    not_signed
  fi
  if ! ov_ask "Sign in to Codex now with 'codex login' (a browser opens)?" 1; then
    WHY=""
    [ "$MODE" = "--from-update" ] && remember
    not_signed
  fi
fi
WHY="login-failed"
codex login
if codex login status >/dev/null 2>&1; then
  WHY=""
  echo "[OK] codex is signed in -- click Codex in the overlay's titlebar to use it."
  rm -f "$DECLINED" 2>/dev/null
  RC=0; finish
fi
not_signed
