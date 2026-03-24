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
SHELL_RC="$HOME/.bashrc"
[[ -n "${ZSH_VERSION:-}" ]] && SHELL_RC="$HOME/.zshrc"
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    if ! grep -q '.local/bin' "$SHELL_RC" 2>/dev/null; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
        ok "Added ~/.local/bin to PATH in $SHELL_RC"
    fi
fi
if [[ ":$PATH:" != *":$HOME/.npm-global/bin:"* ]]; then
    if ! grep -q '.npm-global/bin' "$SHELL_RC" 2>/dev/null; then
        echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> "$SHELL_RC"
        ok "Added ~/.npm-global/bin to PATH in $SHELL_RC"
    fi
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

mkdir -p context

printf "\n"

# ── 6. Project selection ──────────────────────────────────────────────────────

printf "  ${BOLD}Project${NC}\n\n"

PROJECT_NAME_ARG="${1:-}"

tty_printf() {
    if [ "$HAS_TTY" = true ]; then
        printf "%b" "$1" >/dev/tty
    else
        printf "%b" "$1" >&2
    fi
}

list_existing_projects() {
    local base="$1"
    local found=0
    if [ -d "$base" ]; then
        while IFS= read -r entry; do
            found=1
            tty_printf "    ${DIM}- ${entry}${NC}\n"
        done < <(ls -1 "$base" 2>/dev/null | sort)
    fi
    [ "$found" -eq 1 ]
}

prompt_project_name() {
    local default_name="${1:-default}"
    local answer=""

    if [ -n "$PROJECT_NAME_ARG" ]; then
        printf "%s" "$PROJECT_NAME_ARG"
        return 0
    fi

    if [ -n "${ARBOS_PROJECT:-}" ]; then
        printf "%s" "$ARBOS_PROJECT"
        return 0
    fi

    if [ "$HAS_TTY" = true ]; then
        tty_printf "  Existing projects under context/:\n"
        if ! list_existing_projects "$INSTALL_DIR/context"; then
            tty_printf "    ${DIM}(none yet)${NC}\n"
        fi
        tty_printf "\n"
        tty_printf "  ${DIM}Press Enter for '${default_name}', or type a new/existing project name.${NC}\n"
        tty_printf "  ${DIM}Non-interactive options: ./run.sh <project> or ARBOS_PROJECT=<project> ./run.sh${NC}\n\n"
        tty_printf "  ${CYAN}Project name${NC} [${default_name}]: "
        read -r answer </dev/tty 2>/dev/null || answer=""
        tty_printf "\n"
    fi

    answer="${answer:-$default_name}"
    answer="${answer#"${answer%%[![:space:]]*}"}"
    answer="${answer%"${answer##*[![:space:]]}"}"
    [ -n "$answer" ] || answer="$default_name"
    printf "%s" "$answer"
}

populate_workspace_repo() {
    local source_path="$1"
    local workspace_dir="$2"
    local repo_name
    local target_path

    [ -d "$source_path" ] || return 0
    repo_name="$(basename "$source_path")"
    target_path="$workspace_dir/$repo_name"

    if [ -L "$target_path" ]; then
        rm -f "$target_path"
    fi
    if [ -e "$target_path" ]; then
        return 0
    fi
    mv "$source_path" "$target_path"
}

populate_project_workspace() {
    local workspace_dir="$1"
    mkdir -p "$workspace_dir"
    populate_workspace_repo "$INSTALL_DIR/agcli" "$workspace_dir"
    populate_workspace_repo "$INSTALL_DIR/taocli" "$workspace_dir"
}

init_project_layout() {
    local project_dir="$1"
    local workspace_dir="$2"

    mkdir -p \
        "$project_dir/runs" \
        "$project_dir/chat" \
        "$project_dir/files" \
        "$project_dir/logs" \
        "$project_dir/.claude" \
        "$workspace_dir"

    touch \
        "$project_dir/.env" \
        "$project_dir/GOAL.md" \
        "$project_dir/STATE.md" \
        "$project_dir/INBOX.md"
}

PROJECT_NAME="$(prompt_project_name "default")"
PROJECT_DIR="$INSTALL_DIR/context/$PROJECT_NAME"
WORKSPACE_DIR="$PROJECT_DIR/workspace"
PROJECT_ENV_FILE="$PROJECT_DIR/.env"
PM2_NAME_FILE="$PROJECT_DIR/.pm2-name"
HEALTH_DEFAULT="$(PROJECT_NAME="$PROJECT_NAME" python3 - <<'PY'
import hashlib
import os
name = os.environ["PROJECT_NAME"]
digest = hashlib.sha1(name.encode("utf-8")).hexdigest()
print(8200 + (int(digest[:4], 16) % 2000))
PY
)"

if [ -d "$PROJECT_DIR" ]; then
    ok "Using existing project at $PROJECT_DIR"
else
    mkdir -p "$PROJECT_DIR"
    ok "Created project at $PROJECT_DIR"
fi

init_project_layout "$PROJECT_DIR" "$WORKSPACE_DIR"
populate_project_workspace "$WORKSPACE_DIR"

printf "\n"

# ── 7. API keys ───────────────────────────────────────────────────────────────

printf "  ${BOLD}API Keys${NC}\n\n"

ask_key() {
    local env_file="$1" key_name="$2" prompt_text="$3" help_text="$4" required="$5" default_value="${6:-}"
    local existing=""

    if [ -f "$env_file" ]; then
        while IFS= read -r line; do
            case "$line" in
                "${key_name}"=*)
                    existing="$line"
                    ;;
            esac
        done < "$env_file"
    fi
    if [ -n "$existing" ]; then
        ok "$key_name already set"
        return 0
    fi

    if [ -n "${!key_name:-}" ]; then
        echo "${key_name}=${!key_name}" >> "$env_file"
        ok "$key_name saved (from environment)"
        return 0
    fi

    if [ "$HAS_TTY" != true ]; then
        if [ "$required" = "required" ]; then
            if [ -n "$default_value" ]; then
                echo "${key_name}=${default_value}" >> "$env_file"
                ok "$key_name saved (default)"
                return 0
            fi
            die "No TTY — set $key_name in project .env or environment and re-run"
        else
            return 0
        fi
    fi

    [ -n "$help_text" ] && printf "  ${DIM}%s${NC}\n\n" "$help_text"
    if [ -n "$default_value" ]; then
        printf "  ${CYAN}%s${NC} [${default_value}]: " "$prompt_text"
    else
        printf "  ${CYAN}%s:${NC} " "$prompt_text"
    fi
    read -r _val </dev/tty 2>/dev/null || _val=""
    _val="${_val:-$default_value}"

    if [ -z "$_val" ]; then
        if [ "$required" = "required" ]; then
            die "$key_name is required"
        else
            ok "$key_name skipped"
            return 0
        fi
    fi

    echo "${key_name}=${_val}" >> "$env_file"
    ok "$key_name saved"
}

ask_key "$PROJECT_ENV_FILE" "OPENROUTER_API_KEY" \
    "OpenRouter API key" \
    "Get yours at: https://openrouter.ai/keys" \
    "required"

printf "\n"

ask_key "$PROJECT_ENV_FILE" "TAU_BOT_TOKEN" \
    "Telegram bot token" \
    "Create a bot via @BotFather on Telegram, then paste the token here" \
    "required"

printf "\n"

ask_key "$PROJECT_ENV_FILE" "ARBOS_HEALTH_PORT" \
    "Health port" \
    "Each project should use its own local health port." \
    "required" \
    "$HEALTH_DEFAULT"

printf "\n"

# ── 8. Start Arbos ───────────────────────────────────────────────────────────

printf "  ${BOLD}Starting Arbos${NC}\n\n"

if ! command_exists claude; then
    die "'claude' command not found in PATH — install via: npm i -g @anthropic-ai/claude-code"
fi
ok "Claude Code found at $(which claude)"

LAUNCH_SCRIPT="$PROJECT_DIR/.arbos-launch.sh"
cat > "$LAUNCH_SCRIPT" <<LAUNCH
#!/usr/bin/env bash
export PATH="\$HOME/.local/bin:\$HOME/.cargo/bin:\$HOME/.npm-global/bin:/usr/local/bin:\$PATH"
cd "$PROJECT_DIR"
set -a; [ -f "$PROJECT_ENV_FILE" ] && source "$PROJECT_ENV_FILE"; set +a
source "$INSTALL_DIR/.venv/bin/activate"
exec arbos -p "$PROJECT_DIR" 2>&1
LAUNCH
chmod +x "$LAUNCH_SCRIPT"

slugify() {
    printf "%s" "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g; s/-\{2,\}/-/g; s/^-//; s/-$//'
}

next_pm2_name() {
    local base="$1"
    local candidate="$base"
    local n=2
    while pm2 describe "$candidate" >/dev/null 2>&1; do
        candidate="${base}-${n}"
        n=$((n+1))
    done
    printf "%s" "$candidate"
}

PROJECT_SLUG="$(slugify "$PROJECT_NAME")"
[ -n "$PROJECT_SLUG" ] || PROJECT_SLUG="project"

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

if [ -f "$PM2_NAME_FILE" ]; then
    PM2_NAME="$(sed -n '1p' "$PM2_NAME_FILE" | tr -d '\r')"
else
    PM2_NAME="$(next_pm2_name "arbos-$PROJECT_SLUG")"
    printf "%s\n" "$PM2_NAME" > "$PM2_NAME_FILE"
fi

if pm2 describe "$PM2_NAME" >/dev/null 2>&1; then
    pm2 delete "$PM2_NAME" 2>/dev/null || true
fi

pm2 start "$LAUNCH_SCRIPT" \
    --name "$PM2_NAME" \
    --cwd "$PROJECT_DIR" \
    --log "$PROJECT_DIR/logs/arbos.log" \
    --time \
    --restart-delay 10000

pm2 save 2>/dev/null || true

# ── Done ─────────────────────────────────────────────────────────────────
printf "\n"
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
printf "  /restart (restart Arbos)\n"
printf "  /env KEY VAL DESC (add env)\n"
printf "\n"

sleep 2
if pm2 pid "$PM2_NAME" >/dev/null 2>&1 && [ -n "$(pm2 pid "$PM2_NAME")" ]; then
    ok "Arbos running"
    pm2 logs "$PM2_NAME"
else
    err "Arbos may not have started — check logs:"
    printf "    ${DIM}pm2 logs $PM2_NAME${NC}\n"
fi

