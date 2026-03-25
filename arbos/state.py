from __future__ import annotations
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from . import runtime as runtime_state
from .env import _redact_secrets

def _save_agent():
    """Persist agent metadata to meta.json.

    Safe to call both inside and outside an existing ``with _agent_lock`` block.
    """
    with runtime_state._agent_lock:
        st = runtime_state._agent.started
        data = {'summary': runtime_state._agent.summary, 'delay_minutes': runtime_state._agent.delay_minutes, 'started': st, 'paused': _paused_persistent(started=st), 'step_count': runtime_state._agent.step_count, 'goal_hash': runtime_state._agent.goal_hash, 'last_run': runtime_state._agent.last_run, 'last_finished': runtime_state._agent.last_finished, 'last_step_ok': runtime_state._agent.last_step_ok, 'last_step_error': runtime_state._agent.last_step_error}
    runtime_state.META_FILE.parent.mkdir(parents=True, exist_ok=True)
    runtime_state.META_FILE.write_text(json.dumps(data, indent=2))

def _load_agent():
    """Load agent metadata from meta.json into _agent."""
    if not runtime_state.META_FILE.exists():
        return
    try:
        info = json.loads(runtime_state.META_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return
    with runtime_state._agent_lock:
        runtime_state._agent.summary = info.get('summary', '')
        if 'delay_minutes' in info:
            runtime_state._agent.delay_minutes = int(info['delay_minutes'])
        else:
            legacy_s = int(info.get('delay', 0))
            runtime_state._agent.delay_minutes = (legacy_s + 59) // 60 if legacy_s > 0 else 0
        runtime_state._agent.started = info.get('started', False)
        runtime_state._agent.step_count = info.get('step_count', 0)
        runtime_state._agent.goal_hash = info.get('goal_hash', '')
        runtime_state._agent.last_run = info.get('last_run', '')
        runtime_state._agent.last_finished = info.get('last_finished', '')
        runtime_state._agent.last_step_ok = info.get('last_step_ok')
        runtime_state._agent.last_step_error = info.get('last_step_error', '')
        runtime_state._agent.paused = _paused_persistent(started=runtime_state._agent.started)

def _format_last_time(iso_ts: str) -> str:
    if not iso_ts:
        return 'never'
    try:
        dt = datetime.fromisoformat(iso_ts)
        secs = (datetime.now() - dt).total_seconds()
        if secs < 60:
            return f'{int(secs)}s ago'
        if secs < 3600:
            return f'{int(secs / 60)}m ago'
        if secs < 86400:
            return f'{int(secs / 3600)}h ago'
        return f'{int(secs / 86400)}d ago'
    except (ValueError, TypeError):
        return 'unknown'

def _paused_persistent(*, started: bool | None=None) -> bool:
    """True when a goal exists but GO.md is absent (intentional pause).

    Pass ``started`` when already holding ``_agent_lock`` to avoid deadlock.
    """
    if started is None:
        with runtime_state._agent_lock:
            st = runtime_state._agent.started
    else:
        st = started
    if not st:
        return False
    if not runtime_state.GOAL_FILE.exists() or not runtime_state.GOAL_FILE.read_text().strip():
        return False
    return not runtime_state.GO_FLAG_FILE.exists()

def _write_go_flag() -> None:
    runtime_state.CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    runtime_state.GO_FLAG_FILE.write_text('# Arbos: run enabled\n# Remove this file (or /pause) to pause the loop without changing GOAL.md.\n')

def _agent_status_label(gs: runtime_state.AgentState) -> str:
    if not gs.started:
        return 'stopped'
    if not runtime_state.GOAL_FILE.exists() or not runtime_state.GOAL_FILE.read_text().strip():
        return 'idle'
    if not runtime_state.GO_FLAG_FILE.exists():
        return 'paused'
    return 'running'

def log_chat(role: str, text: str):
    """Append to chatlog, rolling to a new file when size exceeds limit."""
    with runtime_state._chatlog_lock:
        runtime_state.CHATLOG_DIR.mkdir(parents=True, exist_ok=True)
        max_file_size = 4000
        max_files = 50
        existing = sorted(runtime_state.CHATLOG_DIR.glob('*.jsonl'))
        current: Path | None = None
        if existing and existing[-1].stat().st_size < max_file_size:
            current = existing[-1]
        if current is None:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            current = runtime_state.CHATLOG_DIR / f'{ts}.jsonl'
        entry = json.dumps({'role': role, 'text': _redact_secrets(text[:1000]), 'ts': datetime.now().isoformat()})
        with open(current, 'a', encoding='utf-8') as f:
            f.write(entry + '\n')
        all_files = sorted(runtime_state.CHATLOG_DIR.glob('*.jsonl'))
        for old in all_files[:-max_files]:
            old.unlink(missing_ok=True)

def load_chatlog(max_chars: int=8000) -> str:
    """Load recent Telegram chat history."""
    if not runtime_state.CHATLOG_DIR.exists():
        return ''
    files = sorted(runtime_state.CHATLOG_DIR.glob('*.jsonl'))
    if not files:
        return ''
    lines: list[str] = []
    total = 0
    for f in reversed(files):
        for raw in reversed(f.read_text().strip().splitlines()):
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            entry = f"[{msg.get('ts', '?')[:16]}] {msg['role']}: {msg['text']}"
            if total + len(entry) > max_chars:
                lines.reverse()
                return '## Recent Telegram chat\n\n' + '\n'.join(lines)
            lines.append(entry)
            total += len(entry) + 1
    lines.reverse()
    if not lines:
        return ''
    return '## Recent Telegram chat\n\n' + '\n'.join(lines)

def make_run_dir() -> Path:
    runtime_state.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    run_dir = runtime_state.RUNS_DIR / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir

def _utc_now_iso() -> str:
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

def _path_for_metadata(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.relative_to(runtime_state.PROJECT_DIR))
    except ValueError:
        return str(path)


def _path_for_display(path: Path | None) -> str:
    if path is None:
        return ''
    try:
        return str(path.relative_to(runtime_state.CODE_DIR))
    except ValueError:
        return str(path)

def _usage_to_dict(usage: runtime_state.ClaudeUsage | None) -> dict[str, int] | None:
    if usage is None:
        return None
    return {'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens, 'cache_creation_input_tokens': usage.cache_creation_input_tokens, 'cache_read_input_tokens': usage.cache_read_input_tokens, 'total_input_tokens': usage.total_input_tokens}

def _summarize_claude_cmd(cmd: list[str]) -> list[str]:
    out: list[str] = []
    skip_prompt = False
    for (i, part) in enumerate(cmd):
        if skip_prompt:
            skip_prompt = False
            continue
        if part == '-p' and i + 1 < len(cmd):
            out.extend(['-p', '<prompt elided>'])
            skip_prompt = True
            continue
        out.append(part if len(part) <= 120 else part[:117] + '...')
    return out

def _invocation_snapshot_entry(meta: dict[str, Any], *, now: float | None=None) -> dict[str, Any]:
    now = time.time() if now is None else now
    item = {k: v for (k, v) in meta.items() if not k.startswith('_')}
    started_wall = float(meta.get('_started_wall', 0.0) or 0.0)
    finished_wall = meta.get('_finished_wall')
    if finished_wall is None:
        item['uptime_seconds'] = max(0, int(now - started_wall)) if started_wall else None
    else:
        item['duration_seconds'] = max(0, int(float(finished_wall) - started_wall))
    return item

def _persist_claude_invocations_locked() -> None:
    now = time.time()
    items = [_invocation_snapshot_entry(meta, now=now) for meta in runtime_state._claude_invocations.values()]
    items.sort(key=lambda item: (item.get('status') != 'running', -(item.get('pid') or 0), item.get('started_at') or ''))
    payload = {'updated_at': _utc_now_iso(), 'arbos_pid': os.getpid(), 'running_count': sum((1 for item in items if item.get('status') == 'running')), 'items': items[:100]}
    runtime_state.CLAUDE_INVOCATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    runtime_state.CLAUDE_INVOCATIONS_FILE.write_text(json.dumps(payload, indent=2))

def _persist_run_invocation_meta(meta: dict[str, Any]) -> None:
    run_dir_str = meta.get('run_dir')
    if not run_dir_str:
        return
    run_dir = runtime_state.PROJECT_DIR / run_dir_str
    run_dir.mkdir(parents=True, exist_ok=True)
    attempt = int(meta.get('attempt') or 0)
    file_name = f'invocation-{attempt}.json' if attempt > 0 else 'invocation.json'
    path = run_dir / file_name
    path.write_text(json.dumps(_invocation_snapshot_entry(meta), indent=2))

def _register_claude_invocation(proc: subprocess.Popen, *, kind: str, phase: str, run_dir: Path | None, attempt: int, cmd: list[str], prompt_est_tokens: int=0, step_label: str | None=None) -> str:
    invocation_id = uuid4().hex[:12]
    now_wall = time.time()
    meta: dict[str, Any] = {'invocation_id': invocation_id, 'status': 'running', 'kind': kind, 'phase': phase, 'step_label': step_label, 'attempt': attempt, 'pid': proc.pid, 'arbos_pid': os.getpid(), 'started_at': _utc_now_iso(), 'finished_at': None, 'last_error': None, 'returncode': None, 'run_dir': _path_for_metadata(run_dir), 'log_path': _path_for_metadata(run_dir / 'logs.txt') if run_dir else None, 'output_path': _path_for_metadata(run_dir / 'output.txt') if run_dir else None, 'rollout_path': _path_for_metadata(run_dir / 'rollout.md') if run_dir else None, 'prompt_est_tokens': prompt_est_tokens, 'usage': None, 'command': _summarize_claude_cmd(cmd), '_started_wall': now_wall, '_finished_wall': None}
    with runtime_state._claude_invocations_lock:
        runtime_state._claude_invocations[invocation_id] = meta
        _persist_claude_invocations_locked()
    _persist_run_invocation_meta(meta)
    return invocation_id

def _finalize_claude_invocation(invocation_id: str | None, *, returncode: int, stderr_output: str, usage: runtime_state.ClaudeUsage | None, status: str | None=None) -> None:
    if not invocation_id:
        return
    with runtime_state._claude_invocations_lock:
        meta = runtime_state._claude_invocations.get(invocation_id)
        if not meta:
            return
        meta['status'] = status or ('done' if returncode == 0 else 'failed')
        meta['returncode'] = returncode
        meta['finished_at'] = _utc_now_iso()
        meta['usage'] = _usage_to_dict(usage)
        meta['last_error'] = (stderr_output or '').strip()[:800] or None
        meta['_finished_wall'] = time.time()
        _persist_claude_invocations_locked()
        snapshot = dict(meta)
    _persist_run_invocation_meta(snapshot)

def _mark_claude_invocation_pid_status(pid: int, *, status: str, detail: str | None=None) -> None:
    if not pid:
        return
    with runtime_state._claude_invocations_lock:
        changed = False
        for meta in runtime_state._claude_invocations.values():
            if meta.get('pid') != pid or meta.get('status') != 'running':
                continue
            meta['status'] = status
            meta['finished_at'] = _utc_now_iso()
            meta['last_error'] = detail[:800] if detail else meta.get('last_error')
            meta['_finished_wall'] = time.time()
            changed = True
            _persist_run_invocation_meta(meta)
        if changed:
            _persist_claude_invocations_locked()

def _claude_invocation_items(*, include_finished: bool=True) -> list[dict[str, Any]]:
    now = time.time()
    with runtime_state._claude_invocations_lock:
        items = [_invocation_snapshot_entry(meta, now=now) for meta in runtime_state._claude_invocations.values() if include_finished or meta.get('status') == 'running']
    items.sort(key=lambda item: item.get('started_at') or '', reverse=True)
    items.sort(key=lambda item: item.get('status') != 'running')
    return items

def _reset_claude_invocations() -> None:
    with runtime_state._claude_invocations_lock:
        runtime_state._claude_invocations.clear()
        _persist_claude_invocations_locked()

def _claude_invocations_prompt_section(limit: int=8) -> str:
    items = _claude_invocation_items()
    registry_path = _path_for_display(runtime_state.CLAUDE_INVOCATIONS_FILE)
    run_meta_path = f'{_path_for_display(runtime_state.RUNS_DIR)}/<timestamp>/invocation-<attempt>.json'
    if not items:
        return f'## Claude invocations\n\nRegistry: `{registry_path}`\nPer-run metadata: `{run_meta_path}`\nNo Claude invocations have been recorded in this process yet.'
    lines = ['## Claude invocations', '', f'Registry: `{registry_path}`', f'Per-run metadata: `{run_meta_path}`', 'Use the `pid` there if you need to inspect or kill a stuck Claude subprocess.', '']
    for item in items[:limit]:
        status = item.get('status') or 'unknown'
        label = item.get('step_label') or item.get('phase') or item.get('kind') or 'claude'
        pid = item.get('pid') or '?'
        age = item.get('uptime_seconds')
        if age is None:
            age = item.get('duration_seconds')
        run_dir = item.get('run_dir') or '(none)'
        lines.append(f'- `{label}` [{status}] pid={pid} age={age}s run_dir=`{run_dir}`')
    if len(items) > limit:
        lines.append(f'- … and {len(items) - limit} more invocation(s).')
    return '\n'.join(lines)
