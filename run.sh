#!/usr/bin/env bash
#
# Arbos — one-command install
#
# Usage:
#   ./run.sh
#   curl -fsSL <url>/run.sh | bash   (interactive)
#

set -e
set -o pipefail

# ── Colors ───────────────────────────────────────────────────────────────────

if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
    GREEN=$'\033[0;32m' RED=$'\033[0;31m' CYAN=$'\033[0;36m'
    BOLD=$'\033[1m' DIM=$'\033[2m' NC=$'\033[0m'
else
    GREEN='' RED='' CYAN='' BOLD='' DIM='' NC=''
fi

ok()  { printf "  ${GREEN}+${NC} %s\n" "$1"; }
err() { printf "  ${RED}x${NC} %s\n" "$1"; }
die() { err "$1"; exit 1; }

command_exists() { command -v "$1" >/dev/null 2>&1; }

# ── Spinner ──────────────────────────────────────────────────────────────────

spin() {
    local pid=$1 msg="$2" i=0 chars='|/-\'
    printf "\033[?25l" 2>/dev/null || true
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r  ${CYAN}%s${NC} %s" "${chars:$((i%4)):1}" "$msg"
        sleep 0.1 2>/dev/null || sleep 1
        i=$((i+1))
    done
    printf "\033[?25h" 2>/dev/null || true
    wait "$pid" 2>/dev/null; local code=$?
    if [ $code -eq 0 ]; then
        printf "\r  ${GREEN}+${NC} %s\n" "$msg"
    else
        printf "\r  ${RED}x${NC} %s\n" "$msg"
    fi
    return $code
}

run() {
    local msg="$1"; shift
    local tmp_out=$(mktemp) tmp_err=$(mktemp)
    "$@" >"$tmp_out" 2>"$tmp_err" &
    local pid=$!
    if ! spin $pid "$msg"; then
        if [ -s "$tmp_err" ]; then
            printf "\n    ${RED}${BOLD}stderr:${NC}\n"
            while IFS= read -r l; do printf "    ${DIM}%s${NC}\n" "$l"; done < "$tmp_err"
        elif [ -s "$tmp_out" ]; then
            printf "\n    ${RED}${BOLD}output:${NC}\n"
            tail -20 "$tmp_out" | while IFS= read -r l; do printf "    ${DIM}%s${NC}\n" "$l"; done
        fi
        printf "\n"
        rm -f "$tmp_out" "$tmp_err"
        return 1
    fi
    rm -f "$tmp_out" "$tmp_err"
}

# ── Detect context ───────────────────────────────────────────────────────────

REPO_URL="https://github.com/unconst/Arbos.git"
INSTALL_DIR=""

if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -z "$INSTALL_DIR" ] || [ ! -f "$INSTALL_DIR/pyproject.toml" ]; then
    INSTALL_DIR="$PWD/Arbos"
fi

HAS_TTY=false
if [ -t 0 ] || { [ -e /dev/tty ] && (echo >/dev/tty) 2>/dev/null; }; then
    HAS_TTY=true
fi

# ── Banner ───────────────────────────────────────────────────────────────────

printf "\n${CYAN}${BOLD}"
printf "      _         _               \n"
printf "     / \\   _ __| |__   ___  ___ \n"
printf "    / _ \\ | '__| '_ \\ / _ \\/ __|\n"
printf "   / ___ \\| |  | |_) | (_) \\__ \\\\\n"
printf "  /_/   \\_\\_|  |_.__/ \\___/|___/\n"
printf "${NC}\n"

# ── 1. Detect package manager ────────────────────────────────────────────────

pkg_install() {
    if command_exists apt-get; then
        sudo apt-get update -qq && sudo apt-get install -y -qq "$@"
    elif command_exists dnf; then
        sudo dnf install -y -q "$@"
    elif command_exists yum; then
        sudo yum install -y -q "$@"
    elif command_exists pacman; then
        sudo pacman -S --noconfirm --needed "$@"
    elif command_exists brew; then
        brew install "$@"
    else
        die "No supported package manager found (apt/dnf/yum/pacman/brew)"
    fi
}

install_node_runtime() {
    if command_exists npm; then
        return 0
    fi

    if command_exists apt-get; then
        sudo apt-get update -qq && sudo apt-get install -y -qq nodejs npm
    elif command_exists dnf; then
        sudo dnf install -y -q nodejs npm
    elif command_exists yum; then
        sudo yum install -y -q nodejs npm
    elif command_exists pacman; then
        sudo pacman -S --noconfirm --needed nodejs npm
    elif command_exists brew; then
        brew install node
    else
        die "No supported package manager found for Node.js/npm install"
    fi
}

npm_global_install() {
    mkdir -p "$HOME/.npm-global"
    npm install -g --prefix "$HOME/.npm-global" "$@"
}

link_global_bin_into_local() {
    local name="$1"
    local src="$HOME/.npm-global/bin/$name"
    local dst="$HOME/.local/bin/$name"
    mkdir -p "$HOME/.local/bin"
    if [ -L "$dst" ] || [ -f "$dst" ]; then
        local current=""
        current="$(readlink "$dst" 2>/dev/null || true)"
        if [ "$current" = "../.npm-global/bin/$name" ] || [ "$current" = "$src" ]; then
            return 0
        fi
    fi
    [ -e "$src" ] || return 0
    ln -sfn "../.npm-global/bin/$name" "$dst"
}

expose_npm_global_bins() {
    local name
    for name in claude pm2 pm2-dev pm2-docker pm2-runtime; do
        link_global_bin_into_local "$name"
    done
}

# ── 2. Install prerequisites ────────────────────────────────────────────────

printf "  ${BOLD}Installing prerequisites${NC}\n\n"

for cmd in git python3 curl; do
    if command_exists "$cmd"; then
        ok "$cmd"
    else
        run "Installing $cmd" pkg_install "$cmd"
        command_exists "$cmd" || die "Failed to install $cmd"
    fi
done

if command_exists npm; then
    ok "npm"
else
    run "Installing Node.js/npm" install_node_runtime
    command_exists npm || die "Failed to install npm"
fi

printf "\n"

# ── 3. Clone repo ───────────────────────────────────────────────────────────

printf "  ${BOLD}Cloning repo${NC}\n\n"

if [ -f "$INSTALL_DIR/pyproject.toml" ]; then
    ok "Project already exists at $INSTALL_DIR"
else
    if [ -d "$INSTALL_DIR" ]; then
        die "$INSTALL_DIR exists but has no pyproject.toml — remove it first or set INSTALL_DIR"
    fi
    run "Cloning $REPO_URL → $INSTALL_DIR" git clone "$REPO_URL" "$INSTALL_DIR"
    [ -f "$INSTALL_DIR/pyproject.toml" ] || die "Clone failed — pyproject.toml not found"
fi

printf "\n"

# ── 4. Install tooling ──────────────────────────────────────────────────────

printf "  ${BOLD}Installing tooling${NC}\n\n"

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$HOME/.npm-global/bin:/usr/local/bin:$PATH"

# uv
if command_exists uv; then
    ok "uv already installed"
else
    run "Installing uv" bash -c "curl -LsSf https://astral.sh/uv/install.sh | sh"
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command_exists uv || die "uv install failed"
fi

# Claude Code CLI
if command_exists claude; then
    ok "Claude Code already installed"
    expose_npm_global_bins
else
    run "Installing Claude Code" npm_global_install @anthropic-ai/claude-code
    expose_npm_global_bins
    command_exists claude || die "'claude' command not found — install via: npm i -g @anthropic-ai/claude-code"
fi

# PATH persistence
ensure_path_export() {
    local rc_file="$1"
    local export_line="$2"
    local label="$3"

    if [ -z "$rc_file" ]; then
        return 0
    fi
    touch "$rc_file"
    if ! grep -Fqx "$export_line" "$rc_file" 2>/dev/null; then
        printf "\n%s\n" "$export_line" >> "$rc_file"
        ok "Added $label to PATH in $rc_file"
    fi
}

BASH_RC="$HOME/.bashrc"
PROFILE_RC="$HOME/.profile"
ZSH_RC="$HOME/.zshrc"

ensure_path_export "$BASH_RC" 'export PATH="$HOME/.local/bin:$PATH"' "~/.local/bin"
ensure_path_export "$BASH_RC" 'export PATH="$HOME/.npm-global/bin:$PATH"' "~/.npm-global/bin"
ensure_path_export "$PROFILE_RC" 'export PATH="$HOME/.npm-global/bin:$PATH"' "~/.npm-global/bin"

if [ -n "${ZSH_VERSION:-}" ] || [ -f "$ZSH_RC" ]; then
    ensure_path_export "$ZSH_RC" 'export PATH="$HOME/.local/bin:$PATH"' "~/.local/bin"
    ensure_path_export "$ZSH_RC" 'export PATH="$HOME/.npm-global/bin:$PATH"' "~/.npm-global/bin"
fi

printf "\n"

# ── 5. Python environment ───────────────────────────────────────────────────

printf "  ${BOLD}Setting up project${NC}\n\n"

cd "$INSTALL_DIR"

if [ ! -d ".venv" ]; then
    run "Creating Python environment" uv venv .venv
else
    ok "Python environment exists"
fi

source .venv/bin/activate
run "Installing dependencies" uv pip install -e .
command -v arbos >/dev/null 2>&1 || die "arbos entrypoint was not installed into the venv"
ok "arbos entrypoint installed"

mkdir -p context

printf "\n"

# ── 6. Bot identity ──────────────────────────────────────────────────────────

printf "  ${BOLD}Bot Identity${NC}\n\n"

[ -z "${1:-}" ] || die "Project names are now derived from the Telegram bot token; run ./run.sh with no project argument."

prompt_value() {
    local key_name="$1" prompt_text="$2" help_text="$3" required="$4" default_value="${5:-}"
    local val="${!key_name:-}"

    if [ -n "$val" ]; then
        printf "%s" "$val"
        return 0
    fi

    if [ "$HAS_TTY" != true ]; then
        if [ "$required" = "required" ]; then
            [ -n "$default_value" ] || die "No TTY — set $key_name in the environment and re-run"
            printf "%s" "$default_value"
            return 0
        fi
        printf "%s" "$default_value"
        return 0
    fi

    # This function is used inside command substitution, so interactive text
    # must go to the TTY instead of stdout or it will be swallowed.
    [ -n "$help_text" ] && printf "  ${DIM}%s${NC}\n\n" "$help_text" >/dev/tty
    if [ -n "$default_value" ]; then
        printf "  ${CYAN}%s${NC} [${default_value}]: " "$prompt_text" >/dev/tty
    else
        printf "  ${CYAN}%s:${NC} " "$prompt_text" >/dev/tty
    fi
    read -r val </dev/tty 2>/dev/null || val=""
    val="${val:-$default_value}"

    if [ -z "$val" ] && [ "$required" = "required" ]; then
        die "$key_name is required"
    fi
    printf "%s" "$val"
}

project_env_value() {
    local project_dir="$1" key_name="$2" env_file="$1/.env"
    [ -f "$env_file" ] || return 0
    (
        set -a
        # shellcheck source=/dev/null
        source "$env_file" >/dev/null 2>&1 || exit 0
        set +a
        eval "printf '%s' \"\${$key_name:-}\""
    )
}

EXISTING_PROJECT_DIRS=()
SELECTED_PROJECT_DIR=""

if [ -d "$INSTALL_DIR/context" ]; then
    for project_dir in "$INSTALL_DIR"/context/*; do
        [ -d "$project_dir" ] || continue
        EXISTING_PROJECT_DIRS+=("$project_dir")
    done
fi

if [ -n "${TAU_BOT_TOKEN:-}" ]; then
    TAU_BOT_TOKEN_VALUE="$TAU_BOT_TOKEN"
elif [ "$HAS_TTY" = true ] && [ ${#EXISTING_PROJECT_DIRS[@]} -gt 0 ]; then
    printf "  ${DIM}Choose an existing bot or create a new one.${NC}\n\n" >/dev/tty
    option_idx=1
    for project_dir in "${EXISTING_PROJECT_DIRS[@]}"; do
        printf "  [%d] %s\n" "$option_idx" "$(basename "$project_dir")" >/dev/tty
        option_idx=$((option_idx + 1))
    done
    printf "  [%d] new token\n\n" "$option_idx" >/dev/tty
    printf "  ${CYAN}Select bot:${NC} " >/dev/tty
    read -r bot_choice </dev/tty 2>/dev/null || bot_choice=""

    case "$bot_choice" in
        ''|*[!0-9]*)
            die "Please enter a number from 1 to $option_idx"
            ;;
    esac

    if [ "$bot_choice" -ge 1 ] && [ "$bot_choice" -lt "$option_idx" ]; then
        SELECTED_PROJECT_DIR="${EXISTING_PROJECT_DIRS[$((bot_choice - 1))]}"
        TAU_BOT_TOKEN_VALUE="$(project_env_value "$SELECTED_PROJECT_DIR" "TAU_BOT_TOKEN")"
        [ -n "$TAU_BOT_TOKEN_VALUE" ] || die "Selected project is missing TAU_BOT_TOKEN in .env"
        ok "Using saved token from $(basename "$SELECTED_PROJECT_DIR")"
    elif [ "$bot_choice" -eq "$option_idx" ]; then
        TAU_BOT_TOKEN_VALUE="$(prompt_value "TAU_BOT_TOKEN" \
            "Telegram bot token" \
            "Create a bot via @BotFather on Telegram, then paste the token here" \
            "required")"
    else
        die "Please enter a number from 1 to $option_idx"
    fi
else
    TAU_BOT_TOKEN_VALUE="$(prompt_value "TAU_BOT_TOKEN" \
        "Telegram bot token" \
        "Create a bot via @BotFather on Telegram, then paste the token here" \
        "required")"
fi

printf "\n\n"

OPENROUTER_DEFAULT_VALUE="${OPENROUTER_API_KEY:-}"
if [ -z "$OPENROUTER_DEFAULT_VALUE" ] && [ -n "$SELECTED_PROJECT_DIR" ]; then
    OPENROUTER_DEFAULT_VALUE="$(project_env_value "$SELECTED_PROJECT_DIR" "OPENROUTER_API_KEY")"
fi

OPENROUTER_API_KEY_VALUE="$(prompt_value "OPENROUTER_API_KEY" \
    "OpenRouter API key" \
    "Get yours at: https://openrouter.ai/keys" \
    "required" \
    "$OPENROUTER_DEFAULT_VALUE")"

printf "\n"

PROJECT_NAME="$(source "$INSTALL_DIR/.venv/bin/activate" && arbos bot-name --bot-token "$TAU_BOT_TOKEN_VALUE")"
[ -n "$PROJECT_NAME" ] || die "Could not resolve bot username from token"

PROJECT_DIR="$INSTALL_DIR/context/$PROJECT_NAME"
WORKSPACE_DIR="$PROJECT_DIR/workspace"
PM2_NAME="arbos-$PROJECT_NAME"

printf "  ${DIM}Canonical bot name: %s${NC}\n\n" "$PROJECT_NAME"

# ── 7. Start Arbos ───────────────────────────────────────────────────────────

printf "  ${BOLD}Starting Arbos${NC}\n\n"

if ! command_exists claude; then
    die "'claude' command not found in PATH — install via: npm i -g @anthropic-ai/claude-code"
fi
ok "Claude Code found at $(which claude)"

# Install pm2 if needed
if ! command_exists pm2; then
    if ! command_exists npm && ! command_exists npx; then
        run "Installing Node.js/npm" install_node_runtime
    fi
    run "Installing pm2" npm_global_install pm2
    expose_npm_global_bins
    command_exists pm2 || die "pm2 install failed"
else
    expose_npm_global_bins
fi

BOOTSTRAP_OUTPUT="$(
    export OPENROUTER_API_KEY="$OPENROUTER_API_KEY_VALUE"
    export TAU_BOT_TOKEN="$TAU_BOT_TOKEN_VALUE"
    source "$INSTALL_DIR/.venv/bin/activate"
    command -v arbos >/dev/null 2>&1 || { echo "arbos entrypoint missing in venv" >&2; exit 1; }
    arbos bootstrap-project
)"

printf "\n"

# ── Done ─────────────────────────────────────────────────────────────────
printf "  ${GREEN}${BOLD}Arbos${NC}\n"
printf "\n"
printf "  Project: %s\n" "$PROJECT_NAME"
printf "  Context: %s\n" "$PROJECT_DIR"
printf "  Workspace: %s\n" "$WORKSPACE_DIR"
printf "  PM2: %s\n" "$PM2_NAME"
printf "\n"
printf "  arbos -p \"%s\"\n" "$PROJECT_DIR"
printf "  /loop GOAL.md\n"
printf "  /pause (pause loop)\n"
printf "  /resume (resume loop)\n"
printf "  /clear (clear loop)\n"
printf "  /delay <mins> (loop delay)\n"
printf "  /new <bot token> (create a fresh sibling bot)\n"
printf "  /restart (restart Arbos)\n"
printf "  /env KEY \"VALUE\" [DESC] (add env; quote values with spaces)\n"
printf "\n"

sleep 2
if pm2 pid "$PM2_NAME" >/dev/null 2>&1 && [ -n "$(pm2 pid "$PM2_NAME")" ]; then
    ok "Arbos running"
    pm2 logs "$PM2_NAME"
else
    err "Arbos may not have started — check logs:"
    printf "    ${DIM}pm2 logs $PM2_NAME${NC}\n"
fi

