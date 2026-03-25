from __future__ import annotations
import os
import re
from pathlib import Path
from . import runtime as runtime_state
from cryptography.fernet import InvalidToken
from .env import _ENV_KEY_NAME_RE, _decrypt_env_content
from .state import _claude_invocations_prompt_section, load_chatlog
_ARBOS_ENV_BUILTIN_DOC: dict[str, str] = {'AGENT_DELAY': 'Extra seconds added to the loop delay between agent steps.', 'ANTHROPIC_API_KEY': 'API key passed into the agent subprocess for LLM calls (from OPENROUTER_API_KEY).', 'ANTHROPIC_AUTH_TOKEN': 'Optional auth token for the API (cleared when using OpenRouter).', 'ANTHROPIC_BASE_URL': 'Anthropic-compatible API base URL (OpenRouter).', 'ARBOS_WORKSPACE_DIR': 'Project workspace subdirectory for checked-out repos and coding work.', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': 'Claude Code setting to reduce background traffic.', 'CLAUDE_MAX_RETRIES': 'Retries when a Claude run fails.', 'CLAUDE_MODEL': f'Model id for Claude Code (default: {runtime_state.DEFAULT_CLAUDE_MODEL}).', 'CLAUDE_TIMEOUT': 'Idle watchdog timeout in seconds for Claude runs; 0 disables.', 'OPENROUTER_API_KEY': 'OpenRouter API key (loaded into the agent as ANTHROPIC_API_KEY).', runtime_state.BOT_USERNAME_ENV: 'Canonical Telegram bot username for this Arbos instance.', 'TAU_BOT_TOKEN': 'Telegram bot token (Arbos host only; not passed to the coding agent subprocess).', 'TELEGRAM_OWNER_ID': 'Telegram user id allowed to control the bot.'}
_ENV_PROMPT_SKIP_KEYS = frozenset({'PATH', 'HOME', 'USER', 'LOGNAME', 'MAIL', 'SHELL', 'PWD', 'OLDPWD', 'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'LC_NUMERIC', 'LC_TIME', 'TERM', 'TERMINAL', 'SHLVL', 'HOSTNAME', 'HOST', 'INVOCATION_ID', 'JOURNAL_STREAM', 'MANPATH', 'LS_COLORS', 'COMP_WORDS', 'COMP_LINE', '_', 'CLUTTER_IM_MODULE', 'SESSION_MANAGER', 'XAUTHORITY', 'DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS'})
_ENV_PROMPT_SKIP_PREFIXES = ('XDG_', 'VSCODE', 'CURSOR_', 'npm_', 'SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY', 'LESSOPEN', 'LESSCLOSE', 'BUNDLED_DEBUGPY', 'PYDEVD_')

def _env_key_suppressed_for_prompt(key: str) -> bool:
    if key in _ENV_PROMPT_SKIP_KEYS:
        return True
    return any((key.startswith(p) for p in _ENV_PROMPT_SKIP_PREFIXES))

def _env_key_looks_config_like(key: str) -> bool:
    if _env_key_suppressed_for_prompt(key):
        return False
    u = key.upper()
    if u.startswith(('CLAUDE_', 'ARBOS_', 'OPENROUTER_', 'TELEGRAM_', 'ANTHROPIC_', 'AGENT_', 'AWS_', 'AZURE_', 'GOOGLE_', 'GCP_', 'GITHUB', 'GITLAB_', 'DOCKER_', 'OPENAI_', 'HF_', 'DATABASE', 'DB_')) or u.startswith('KUBECONFIG'):
        return True
    needles = ('API_KEY', '_API_KEY', '_TOKEN', '_SECRET', 'SECRET_', 'PASSWORD', 'CREDENTIAL', 'WEBHOOK', 'ENDPOINT', '_KEY', 'AUTH')
    return any((n in u for n in needles))

def _read_project_dotenv_plaintext() -> str | None:
    """Raw .env text for metadata (comments + keys); None if unavailable."""
    if runtime_state.ENV_FILE.exists():
        return runtime_state.ENV_FILE.read_text()
    if not runtime_state.ENV_ENC_FILE.exists():
        return None
    tok = os.environ.get('TAU_BOT_TOKEN', '')
    if not tok:
        return None
    try:
        return _decrypt_env_content(tok)
    except InvalidToken:
        return None

def _parse_dotenv_key_comment_map(content: str) -> tuple[dict[str, str], set[str]]:
    """Map assignment keys to preceding full-line # comments; also return all assignment keys."""
    desc: dict[str, str] = {}
    all_keys: set[str] = set()
    pending: list[str] = []
    for raw in content.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith('#'):
            pending.append(s[1:].strip())
            continue
        assign = s.split('#', 1)[0].strip()
        if '=' not in assign:
            pending.clear()
            continue
        key = assign.split('=', 1)[0].strip()
        if not _ENV_KEY_NAME_RE.match(key):
            pending.clear()
            continue
        all_keys.add(key)
        joined = '; '.join((x for x in pending if x))
        if joined:
            desc[key] = joined
        pending.clear()
    return (desc, all_keys)

def _agent_subprocess_env_keys() -> set[str]:
    """Env keys visible to the Claude Code subprocess (matches _claude_env)."""
    keys = set(os.environ.keys())
    keys.discard('TAU_BOT_TOKEN')
    keys.update(('ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN'))
    return keys

def _prompt_env_keys_to_show(agent_keys: set[str], dotenv_keys: set[str]) -> list[str]:
    show: set[str] = set()
    show |= agent_keys & dotenv_keys
    for k in agent_keys:
        if k in _ARBOS_ENV_BUILTIN_DOC or _env_key_looks_config_like(k):
            show.add(k)
    return sorted(show)

def format_available_env_vars_section(*, max_keys: int=120) -> str:
    """Human-readable KEY + description for the agent (never values)."""
    agent_keys = _agent_subprocess_env_keys()
    raw = _read_project_dotenv_plaintext()
    file_desc: dict[str, str] = {}
    dotenv_keys: set[str] = set()
    if raw:
        (file_desc, dotenv_keys) = _parse_dotenv_key_comment_map(raw)
    all_keys = _prompt_env_keys_to_show(agent_keys, dotenv_keys)
    truncated = len(all_keys) > max_keys
    keys = all_keys[:max_keys]
    lines = ['## Environment variables available to you', '', 'These keys exist in your agent subprocess (via the Arbos host). Values are never listed here — use them as needed; do not print secrets.', '']
    for k in keys:
        desc = file_desc.get(k) or _ARBOS_ENV_BUILTIN_DOC.get(k)
        if not desc:
            desc = 'Present in the agent environment (no description in project env file).'
        lines.append(f'- `{k}`: {desc}')
    if truncated:
        lines.append('')
        lines.append(f'(… and {len(all_keys) - max_keys} more keys not shown.)')
    return '\n'.join(lines)

def _path_for_display(path: Path | None) -> str:
    if path is None:
        return ''
    try:
        return str(path.relative_to(runtime_state.CODE_DIR))
    except ValueError:
        return str(path)

def _prompt_placeholders() -> dict[str, str]:
    return {'{{ARBOS_ROOT_DIR}}': str(runtime_state.CODE_DIR), '{{ARBOS_PROJECT_NAME}}': runtime_state.PROJECT_NAME, '{{ARBOS_PROJECT_DIR}}': str(runtime_state.PROJECT_DIR), '{{ARBOS_CONTEXT_DIR}}': str(runtime_state.CONTEXT_DIR), '{{ARBOS_WORKSPACE_DIR}}': str(runtime_state.WORKSPACE_DIR), '{{ARBOS_INSTANCE_NAME}}': runtime_state.INSTANCE_NAME, '{{ARBOS_ENV_KEYS_SECTION}}': format_available_env_vars_section()}

def _render_prompt_template(text: str) -> tuple[str, bool]:
    rendered = text
    used_env_placeholder = '{{ARBOS_ENV_KEYS_SECTION}}' in rendered
    for (placeholder, value) in _prompt_placeholders().items():
        rendered = rendered.replace(placeholder, value)
    return (rendered, used_env_placeholder)

def load_prompt(consume_inbox: bool=False, agent_step: int=0) -> str:
    """Build full prompt: PROMPT.md + GOAL/STATE/INBOX + chatlog."""
    parts = []
    prompt_has_env_placeholder = False
    if runtime_state.PROMPT_FILE.exists():
        text = runtime_state.PROMPT_FILE.read_text().strip()
        if text:
            (rendered, prompt_has_env_placeholder) = _render_prompt_template(text)
            if rendered.strip():
                parts.append(rendered.strip())
    if not prompt_has_env_placeholder:
        parts.append(format_available_env_vars_section())
    if runtime_state.GOAL_FILE.exists():
        goal_text = runtime_state.GOAL_FILE.read_text().strip()
        if goal_text:
            header = f'## Loop (step {agent_step})' if agent_step else '## Loop'
            context_root = _path_for_display(runtime_state.CONTEXT_DIR)
            workspace_root = _path_for_display(runtime_state.WORKSPACE_DIR)
            parts.append(f'{header}\n\n{goal_text}\n\nYour context files are under `{context_root}/`: STATE.md, INBOX.md, runs/, chat/, files/, and invocations.json. Your coding workspace is `{workspace_root}/`. (see PROMPT.md).')
    if runtime_state.STATE_FILE.exists():
        state_text = runtime_state.STATE_FILE.read_text().strip()
        if state_text:
            parts.append(f'## State\n\n{state_text}')
    if runtime_state.INBOX_FILE.exists():
        inbox_text = runtime_state.INBOX_FILE.read_text().strip()
        if inbox_text:
            parts.append(f'## Inbox\n\n{inbox_text}')
        if consume_inbox:
            runtime_state.INBOX_FILE.write_text('')
    chatlog = load_chatlog()
    if chatlog:
        parts.append(chatlog)
    parts.append(_claude_invocations_prompt_section())
    return '\n\n'.join(parts)
