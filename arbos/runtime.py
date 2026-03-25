from __future__ import annotations
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
import requests
runtime_state = sys.modules[__name__]
CODE_DIR = Path(__file__).resolve().parent.parent
PROMPT_FILE = CODE_DIR / 'PROMPT.md'
PROJECTS_ROOT = CODE_DIR / 'context'
LOCKS_ROOT = PROJECTS_ROOT / '.locks'
TOKEN_LOCKS_ROOT = LOCKS_ROOT / 'tokens'
INSTANCE_LOCKS_ROOT = LOCKS_ROOT / 'instances'
PM2_NAME_FILENAME = '.pm2-name'
LAUNCH_SCRIPT_NAME = '.arbos-launch.sh'
BOT_USERNAME_ENV = 'ARBOS_BOT_USERNAME'
ARBOS_STEP_STREAM_ID_ENV = 'ARBOS_STEP_STREAM_ID'
STATE_AUTOSYNC_START = '<!-- ARBOS_STATE_AUTOSYNC_START -->'
STATE_AUTOSYNC_END = '<!-- ARBOS_STATE_AUTOSYNC_END -->'
FORK_SHARED_ENV_KEYS = ('OPENROUTER_API_KEY', 'CLAUDE_MODEL', 'CLAUDE_MAX_RETRIES', 'CLAUDE_TIMEOUT', 'AGENT_DELAY')
DEFAULT_CLAUDE_MODEL = 'openai/gpt-5.4'

@dataclass(frozen=True)
class BotIdentity:
    username: str
    bot_id: int | None = None
    display_name: str = ''

@dataclass(frozen=True)
class BootstrapResult:
    identity: BotIdentity
    project_dir: Path
    env_file: Path
    pm2_name: str
    launch_script: Path

@dataclass(frozen=True)
class InstancePaths:
    project_name: str
    instance_name: str
    project_dir: Path
    context_dir: Path
    workspace_dir: Path
    goal_file: Path
    go_flag_file: Path
    state_file: Path
    inbox_file: Path
    runs_dir: Path
    meta_file: Path
    claude_invocations_file: Path
    step_msg_file: Path
    context_logs_dir: Path
    chatlog_dir: Path
    files_dir: Path
    restart_flag: Path
    chat_id_file: Path
    operator_thread_id_file: Path
    env_file: Path
    env_enc_file: Path
    env_pending_file: Path
    claude_settings_dir: Path

def _resolve_project_dir(project_arg: str | None) -> tuple[str, Path]:
    raw = (project_arg or os.environ.get('ARBOS_PROJECT') or 'default').strip()
    if not raw:
        raw = 'default'
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        project_dir = candidate.resolve()
        project_name = project_dir.name
    elif '/' in raw or raw.startswith('.'):
        project_dir = (CODE_DIR / candidate).resolve()
        project_name = project_dir.name
    else:
        project_name = raw
        project_dir = (PROJECTS_ROOT / raw).resolve()
    return (project_name, project_dir)

def _build_instance_paths(project_arg: str | None) -> InstancePaths:
    (project_name, project_dir) = _resolve_project_dir(project_arg)
    context_dir = project_dir
    workspace_dir = project_dir / 'workspace'
    return InstancePaths(project_name=project_name, instance_name=project_name, project_dir=project_dir, context_dir=context_dir, workspace_dir=workspace_dir, goal_file=context_dir / 'GOAL.md', go_flag_file=context_dir / 'GO.md', state_file=context_dir / 'STATE.md', inbox_file=context_dir / 'INBOX.md', runs_dir=context_dir / 'runs', meta_file=context_dir / 'meta.json', claude_invocations_file=context_dir / 'invocations.json', step_msg_file=context_dir / '.step_msg', context_logs_dir=context_dir / 'logs', chatlog_dir=context_dir / 'chat', files_dir=context_dir / 'files', restart_flag=context_dir / '.restart', chat_id_file=context_dir / 'chat_id.txt', operator_thread_id_file=context_dir / 'operator_thread_id.txt', env_file=context_dir / '.env', env_enc_file=context_dir / '.env.enc', env_pending_file=context_dir / '.env.pending', claude_settings_dir=context_dir / '.claude')

def _sanitize_instance_name(raw: str) -> str:
    text = (raw or '').strip().lstrip('@').lower()
    text = re.sub('[^a-z0-9_]+', '-', text)
    text = re.sub('-{2,}', '-', text).strip('-')
    if not text:
        raise ValueError('Could not derive a valid bot username for this token.')
    return text

def _pm2_name_for_instance(instance_name: str) -> str:
    return f'arbos-{instance_name}'

def _launch_script_path(project_dir: Path) -> Path:
    return project_dir / LAUNCH_SCRIPT_NAME

def _pm2_name_file(project_dir: Path) -> Path:
    return project_dir / PM2_NAME_FILENAME
CODE_DIR = Path(__file__).resolve().parent.parent
PROMPT_FILE = CODE_DIR / 'PROMPT.md'
PROJECTS_ROOT = CODE_DIR / 'context'
LOCKS_ROOT = PROJECTS_ROOT / '.locks'
TOKEN_LOCKS_ROOT = LOCKS_ROOT / 'tokens'
INSTANCE_LOCKS_ROOT = LOCKS_ROOT / 'instances'
PM2_NAME_FILENAME = '.pm2-name'
LAUNCH_SCRIPT_NAME = '.arbos-launch.sh'
BOT_USERNAME_ENV = 'ARBOS_BOT_USERNAME'
ARBOS_STEP_STREAM_ID_ENV = 'ARBOS_STEP_STREAM_ID'
STATE_AUTOSYNC_START = '<!-- ARBOS_STATE_AUTOSYNC_START -->'
STATE_AUTOSYNC_END = '<!-- ARBOS_STATE_AUTOSYNC_END -->'
FORK_SHARED_ENV_KEYS = ('OPENROUTER_API_KEY', 'CLAUDE_MODEL', 'CLAUDE_MAX_RETRIES', 'CLAUDE_TIMEOUT', 'AGENT_DELAY')
DEFAULT_CLAUDE_MODEL = 'openai/gpt-5.4'

@dataclass(frozen=True)
class BotIdentity:
    username: str
    bot_id: int | None = None
    display_name: str = ''

@dataclass(frozen=True)
class BootstrapResult:
    identity: BotIdentity
    project_dir: Path
    env_file: Path
    pm2_name: str
    launch_script: Path

@dataclass(frozen=True)
class InstancePaths:
    project_name: str
    instance_name: str
    project_dir: Path
    context_dir: Path
    workspace_dir: Path
    goal_file: Path
    go_flag_file: Path
    state_file: Path
    inbox_file: Path
    runs_dir: Path
    meta_file: Path
    claude_invocations_file: Path
    step_msg_file: Path
    context_logs_dir: Path
    chatlog_dir: Path
    files_dir: Path
    restart_flag: Path
    chat_id_file: Path
    operator_thread_id_file: Path
    env_file: Path
    env_enc_file: Path
    env_pending_file: Path
    claude_settings_dir: Path

def _resolve_project_dir(project_arg: str | None) -> tuple[str, Path]:
    raw = (project_arg or os.environ.get('ARBOS_PROJECT') or 'default').strip()
    if not raw:
        raw = 'default'
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        project_dir = candidate.resolve()
        project_name = project_dir.name
    elif '/' in raw or raw.startswith('.'):
        project_dir = (CODE_DIR / candidate).resolve()
        project_name = project_dir.name
    else:
        project_name = raw
        project_dir = (PROJECTS_ROOT / raw).resolve()
    return (project_name, project_dir)

def _build_instance_paths(project_arg: str | None) -> InstancePaths:
    (project_name, project_dir) = _resolve_project_dir(project_arg)
    context_dir = project_dir
    workspace_dir = project_dir / 'workspace'
    return InstancePaths(project_name=project_name, instance_name=project_name, project_dir=project_dir, context_dir=context_dir, workspace_dir=workspace_dir, goal_file=context_dir / 'GOAL.md', go_flag_file=context_dir / 'GO.md', state_file=context_dir / 'STATE.md', inbox_file=context_dir / 'INBOX.md', runs_dir=context_dir / 'runs', meta_file=context_dir / 'meta.json', claude_invocations_file=context_dir / 'invocations.json', step_msg_file=context_dir / '.step_msg', context_logs_dir=context_dir / 'logs', chatlog_dir=context_dir / 'chat', files_dir=context_dir / 'files', restart_flag=context_dir / '.restart', chat_id_file=context_dir / 'chat_id.txt', operator_thread_id_file=context_dir / 'operator_thread_id.txt', env_file=context_dir / '.env', env_enc_file=context_dir / '.env.enc', env_pending_file=context_dir / '.env.pending', claude_settings_dir=context_dir / '.claude')

def _sanitize_instance_name(raw: str) -> str:
    text = (raw or '').strip().lstrip('@').lower()
    text = re.sub('[^a-z0-9_]+', '-', text)
    text = re.sub('-{2,}', '-', text).strip('-')
    if not text:
        raise ValueError('Could not derive a valid bot username for this token.')
    return text

def _pm2_name_for_instance(instance_name: str) -> str:
    return f'arbos-{instance_name}'

def _launch_script_path(project_dir: Path) -> Path:
    return project_dir / LAUNCH_SCRIPT_NAME

def _pm2_name_file(project_dir: Path) -> Path:
    return project_dir / PM2_NAME_FILENAME
INSTANCE_PATHS = _build_instance_paths(None)
PROJECT_NAME = INSTANCE_PATHS.project_name
INSTANCE_NAME = INSTANCE_PATHS.instance_name
PROJECT_DIR = INSTANCE_PATHS.project_dir
CONTEXT_DIR = INSTANCE_PATHS.context_dir
WORKSPACE_DIR = INSTANCE_PATHS.workspace_dir
GOAL_FILE = INSTANCE_PATHS.goal_file
GO_FLAG_FILE = INSTANCE_PATHS.go_flag_file
STATE_FILE = INSTANCE_PATHS.state_file
INBOX_FILE = INSTANCE_PATHS.inbox_file
RUNS_DIR = INSTANCE_PATHS.runs_dir
META_FILE = INSTANCE_PATHS.meta_file
CLAUDE_INVOCATIONS_FILE = INSTANCE_PATHS.claude_invocations_file
STEP_MSG_FILE = INSTANCE_PATHS.step_msg_file
CONTEXT_LOGS_DIR = INSTANCE_PATHS.context_logs_dir
CHATLOG_DIR = INSTANCE_PATHS.chatlog_dir
FILES_DIR = INSTANCE_PATHS.files_dir
RESTART_FLAG = INSTANCE_PATHS.restart_flag
CHAT_ID_FILE = INSTANCE_PATHS.chat_id_file
OPERATOR_THREAD_ID_FILE = INSTANCE_PATHS.operator_thread_id_file
ENV_FILE = INSTANCE_PATHS.env_file
ENV_ENC_FILE = INSTANCE_PATHS.env_enc_file
ENV_PENDING_FILE = INSTANCE_PATHS.env_pending_file
CLAUDE_SETTINGS_DIR = INSTANCE_PATHS.claude_settings_dir

def _apply_instance_paths(paths: InstancePaths) -> None:
    global INSTANCE_PATHS, PROJECT_NAME, INSTANCE_NAME, PROJECT_DIR, CONTEXT_DIR
    global WORKSPACE_DIR
    global GOAL_FILE, GO_FLAG_FILE, STATE_FILE, INBOX_FILE, RUNS_DIR, META_FILE
    global CLAUDE_INVOCATIONS_FILE, STEP_MSG_FILE, CONTEXT_LOGS_DIR, CHATLOG_DIR
    global FILES_DIR, RESTART_FLAG, CHAT_ID_FILE, OPERATOR_THREAD_ID_FILE
    global ENV_FILE, ENV_ENC_FILE, ENV_PENDING_FILE, CLAUDE_SETTINGS_DIR
    INSTANCE_PATHS = paths
    PROJECT_NAME = paths.project_name
    INSTANCE_NAME = paths.instance_name
    PROJECT_DIR = paths.project_dir
    CONTEXT_DIR = paths.context_dir
    WORKSPACE_DIR = paths.workspace_dir
    GOAL_FILE = paths.goal_file
    GO_FLAG_FILE = paths.go_flag_file
    STATE_FILE = paths.state_file
    INBOX_FILE = paths.inbox_file
    RUNS_DIR = paths.runs_dir
    META_FILE = paths.meta_file
    CLAUDE_INVOCATIONS_FILE = paths.claude_invocations_file
    STEP_MSG_FILE = paths.step_msg_file
    CONTEXT_LOGS_DIR = paths.context_logs_dir
    CHATLOG_DIR = paths.chatlog_dir
    FILES_DIR = paths.files_dir
    RESTART_FLAG = paths.restart_flag
    CHAT_ID_FILE = paths.chat_id_file
    OPERATOR_THREAD_ID_FILE = paths.operator_thread_id_file
    ENV_FILE = paths.env_file
    ENV_ENC_FILE = paths.env_enc_file
    ENV_PENDING_FILE = paths.env_pending_file
    CLAUDE_SETTINGS_DIR = paths.claude_settings_dir
CLAUDE_MODEL = DEFAULT_CLAUDE_MODEL
LLM_API_KEY = ''
LLM_BASE_URL = 'https://openrouter.ai/api'
IS_ROOT = os.getuid() == 0
MAX_RETRIES = 5
CLAUDE_TIMEOUT = 600
CLAUDE_IDLE_KILL = True
_shutdown = threading.Event()
_step_count = 0
_context_est_tokens = 0
_context_paid_usage = None
_context_attempt_inflight = False
_context_lock = threading.Lock()
_child_procs: set[subprocess.Popen] = set()
_child_procs_lock = threading.Lock()
_claude_invocations: dict[str, dict[str, Any]] = {}
_claude_invocations_lock = threading.Lock()

@dataclass
class ClaudeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        return max(0, self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens)

    def plus(self, other: 'ClaudeUsage | None') -> 'ClaudeUsage':
        if other is None:
            return ClaudeUsage(input_tokens=self.input_tokens, output_tokens=self.output_tokens, cache_creation_input_tokens=self.cache_creation_input_tokens, cache_read_input_tokens=self.cache_read_input_tokens)
        return ClaudeUsage(input_tokens=self.input_tokens + other.input_tokens, output_tokens=self.output_tokens + other.output_tokens, cache_creation_input_tokens=self.cache_creation_input_tokens + other.cache_creation_input_tokens, cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens)

@dataclass
class AgentState:
    summary: str = ''
    delay_minutes: int = 0
    started: bool = False
    paused: bool = False
    step_count: int = 0
    goal_hash: str = ''
    last_run: str = ''
    last_finished: str = ''
    last_step_ok: bool | None = None
    last_step_error: str = ''
    thread: threading.Thread | None = field(default=None, repr=False)
    wake: threading.Event = field(default_factory=threading.Event, repr=False)
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
_agent = AgentState()
_agent_lock = threading.RLock()
_pending_step_msg_id: int | None = None
_pending_step_msg_lock = threading.Lock()
_arbos_boot_wall: float = 0.0
_operator_lock = threading.Lock()
_operator: dict[str, Any] = {'phase': 'boot', 'detail': '', 'last_tick_wall': 0.0, 'last_error': ''}

def _reload_runtime_config() -> None:
    runtime_state.CLAUDE_MODEL = os.environ.get('CLAUDE_MODEL', runtime_state.DEFAULT_CLAUDE_MODEL)
    runtime_state.LLM_API_KEY = os.environ.get('OPENROUTER_API_KEY', '')
    try:
        runtime_state.MAX_RETRIES = int(os.environ.get('CLAUDE_MAX_RETRIES', '5'))
    except ValueError:
        runtime_state.MAX_RETRIES = 5
    try:
        timeout = int(os.environ.get('CLAUDE_TIMEOUT', '600').strip())
    except ValueError:
        timeout = 600
    if timeout < 0:
        timeout = 600
    runtime_state.CLAUDE_TIMEOUT = timeout
    runtime_state.CLAUDE_IDLE_KILL = runtime_state.CLAUDE_TIMEOUT > 0

def _runtime_instance_name() -> str:
    from .bootstrap import _bot_identity_from_env, _resolve_bot_identity
    bot_name = _bot_identity_from_env()
    if bot_name:
        return bot_name
    token = os.environ.get('TAU_BOT_TOKEN', '').strip()
    if not token:
        return runtime_state.PROJECT_DIR.name
    identity = _resolve_bot_identity(token)
    os.environ[runtime_state.BOT_USERNAME_ENV] = identity.username
    return identity.username

def _validate_runtime_identity() -> None:
    expected_name = _runtime_instance_name()
    actual_name = runtime_state.PROJECT_DIR.name
    if expected_name and actual_name != expected_name:
        raise RuntimeError(f'Context directory must match the bot username: expected `{expected_name}`, got `{actual_name}`. Run `arbos -p <current-project> migrate-bot-names` first.')

def _acquire_runtime_singleton_locks() -> None:
    token = os.environ.get('TAU_BOT_TOKEN', '').strip()
    instance_name = _runtime_instance_name()
    if token:
        runtime_state._token_lock_fh = _acquire_singleton_lock(runtime_state.TOKEN_LOCKS_ROOT, token, 'Telegram bot token')
    if instance_name:
        runtime_state._instance_lock_fh = _acquire_singleton_lock(runtime_state.INSTANCE_LOCKS_ROOT, instance_name, 'bot identity')

def _lock_path(root: Path, key: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(key.encode('utf-8')).hexdigest()
    return root / f'{digest}.lock'

def _acquire_singleton_lock(root: Path, key: str, label: str):
    lock_path = _lock_path(root, key)
    fh = open(lock_path, 'a+', encoding='utf-8')
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        raise RuntimeError(f'Another Arbos process is already using this {label}.') from exc
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps({'pid': os.getpid(), 'label': label, 'instance': runtime_state.PROJECT_DIR.name if 'PROJECT_DIR' in globals() else '', 'ts': datetime.now().isoformat()}, indent=2))
    fh.flush()
    return fh

def _operator_set(phase: str, detail: str='', *, last_error: str | None=None) -> None:
    with runtime_state._operator_lock:
        runtime_state._operator['phase'] = phase
        runtime_state._operator['detail'] = detail
        runtime_state._operator['last_tick_wall'] = time.time()
        if last_error is not None:
            runtime_state._operator['last_error'] = last_error[:800]

def _operator_tick() -> None:
    with runtime_state._operator_lock:
        runtime_state._operator['last_tick_wall'] = time.time()

def _operator_health_payload() -> dict[str, Any]:
    """JSON-serializable snapshot for operators."""
    from .state import _agent_status_label, _claude_invocation_items, _path_for_metadata
    now = time.time()
    with runtime_state._operator_lock:
        tick = float(runtime_state._operator['last_tick_wall'])
        op = {'phase': runtime_state._operator['phase'], 'detail': runtime_state._operator['detail'], 'seconds_since_activity': max(0, int(now - tick)), 'last_error': runtime_state._operator['last_error'] or None}
    boot = runtime_state._arbos_boot_wall or now
    out: dict[str, Any] = {'status': 'ok', 'uptime_seconds': int(now - boot), 'operator': op, 'agent': {}}
    with runtime_state._agent_lock:
        gs = runtime_state._agent
        out['agent'] = {'state': _agent_status_label(gs), 'go': runtime_state.GO_FLAG_FILE.exists(), 'delay_minutes': gs.delay_minutes, 'step_count': gs.step_count, 'last_step_ok': gs.last_step_ok, 'last_run': gs.last_run or None, 'last_finished': gs.last_finished or None, 'last_step_error': gs.last_step_error[:240] + '…' if len(gs.last_step_error) > 240 else gs.last_step_error or None, 'summary': gs.summary or None}
    invocations = _claude_invocation_items()
    out['claude_invocations'] = {'running_count': sum((1 for item in invocations if item.get('status') == 'running')), 'items': invocations[:20], 'registry_path': _path_for_metadata(runtime_state.CLAUDE_INVOCATIONS_FILE)}
    if not runtime_state.LLM_API_KEY:
        out['status'] = 'degraded'
        out['degraded_reason'] = 'OPENROUTER_API_KEY not set — LLM calls will fail'
    return out
