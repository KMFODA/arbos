from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from . import runtime as runtime_state
from .claude import _claude_cmd, _summarize_step_outcome, extract_text, run_agent
from .env import _redact_secrets
from .logs import _approx_prompt_context_tokens, _log, _reset_prompt_context_usage, _safe_int, _step_response_header, fmt_duration
from .runtime import _operator_set, _operator_tick
from .prompts import _path_for_display, load_prompt
from .state import _mark_claude_invocation_pid_status, _reset_claude_invocations, _save_agent, _utc_now_iso, log_chat, make_run_dir
from .telegram import TELEGRAM_SAFE_TEXT, _build_agent_failure_detail, _edit_telegram_text, _send_telegram_new, _send_telegram_text, _step_update_target, _tail_text_for_telegram, _truncate_telegram_text

def _strip_state_autosync_block(text: str) -> str:
    """Remove the host-managed STATE.md footer while preserving agent notes."""
    raw = text or ''
    start = raw.find(runtime_state.STATE_AUTOSYNC_START)
    if start < 0:
        return raw.strip()
    end = raw.find(runtime_state.STATE_AUTOSYNC_END, start)
    if end < 0:
        return raw[:start].strip()
    end += len(runtime_state.STATE_AUTOSYNC_END)
    return (raw[:start] + raw[end:]).strip()

def _state_sync_summary_line(text: str, *, max_chars: int=220) -> str:
    """Pick a compact, single-line summary from agent output."""
    for raw_line in (text or '').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('```'):
            continue
        line = re.sub('^#+\\s+', '', line)
        line = re.sub('^[-*+]\\s+', '', line)
        line = re.sub('^\\d+[.)]\\s+', '', line)
        line = re.sub('\\s+', ' ', line).strip()
        if not line:
            continue
        if len(line) > max_chars:
            return line[:max_chars - 1].rstrip() + '…'
        return line
    return ''

def _sync_state_after_step(*, step_label: str, success: bool, elapsed_s: float, rollout_text: str, failure_detail: str) -> str:
    """Keep STATE.md fresh even if the agent forgets to update it."""
    runtime_state.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing_text = runtime_state.STATE_FILE.read_text() if runtime_state.STATE_FILE.exists() else ''
    preserved_text = _strip_state_autosync_block(existing_text)
    summary = _state_sync_summary_line(rollout_text if rollout_text.strip() else failure_detail)
    if not summary and failure_detail.strip():
        summary = _state_sync_summary_line(failure_detail)
    host_lines = [runtime_state.STATE_AUTOSYNC_START, f'Host sync: {_utc_now_iso()}', f"Last step: {step_label} [{('ok' if success else 'failed')}] ({fmt_duration(elapsed_s)})"]
    if summary:
        host_lines.append(f'Summary: {summary}')
    host_lines.append(runtime_state.STATE_AUTOSYNC_END)
    host_block = '\n'.join(host_lines)
    new_text = f'{preserved_text}\n\n{host_block}'.strip() + '\n'
    runtime_state.STATE_FILE.write_text(new_text)
    return new_text.strip()

def _format_step_live_bubble(elapsed_s: float, step_label: str, rollout: str, tool_or_status: str, *, placeholder: str | None=None) -> str:
    """Single Telegram bubble while a loop step runs: header, optional tool line, rolling rollout tail."""
    header = _step_response_header(elapsed_s, step_label)
    lines = [header]
    ts = (tool_or_status or '').strip()
    if ts and ts not in ('working ...',):
        lines.append(ts)
    prefix = '\n'.join(lines)
    body = rollout.strip()
    if not body:
        body = placeholder or '(waiting for assistant text…)'
    available = TELEGRAM_SAFE_TEXT - len(prefix) - 2
    body = _tail_text_for_telegram(body, max(0, available))
    if body:
        return f'{prefix}\n{body}'
    return prefix

def _pre_send_normal_step_bubble(agent_step: int, global_step: int) -> int | None:
    """Send the step bubble before load_prompt so Telegram shows a new message immediately."""
    target = _step_update_target()
    if not target:
        return None
    step_label = f'Step #{agent_step}' if agent_step else f'Step #{global_step}'
    initial = _format_step_live_bubble(0.0, step_label, '', '', placeholder='(starting…)')
    mid = _send_telegram_new(initial, target=target)
    if mid:
        runtime_state.STEP_MSG_FILE.parent.mkdir(parents=True, exist_ok=True)
        runtime_state.STEP_MSG_FILE.write_text(json.dumps({'msg_id': mid, 'text': initial}))
    else:
        _log('pre_send step bubble: sendMessage returned no message_id')
    return mid

def run_step(prompt: str, step_number: int, agent_step: int=0, *, force_step: bool=False, existing_step_msg_id: int | None=None) -> tuple[bool, str]:
    run_dir: Path | None = None
    t0 = time.monotonic()
    _reset_prompt_context_usage(prompt)
    smf = runtime_state.STEP_MSG_FILE
    use_step_msg_file = not force_step
    if force_step:
        step_label = 'Step (forced)'
        _operator_set('agent_step', 'forced step — Claude running')
    else:
        step_label = f'Step #{agent_step}' if agent_step else f'Step #{step_number}'
    target = _step_update_target()
    step_msg_id: int | None = None
    step_msg_text = ''
    last_edit = 0.0
    step_stream_id = uuid4().hex[:12]
    _step_initial_text = _format_step_live_bubble(0.0, step_label, '', '', placeholder='(starting…)')
    if existing_step_msg_id is not None and (not force_step):
        step_msg_id = existing_step_msg_id
        if use_step_msg_file:
            smf.parent.mkdir(parents=True, exist_ok=True)
            smf.write_text(json.dumps({'msg_id': step_msg_id, 'text': _step_initial_text, 'stream_id': step_stream_id}))
    elif target:
        step_msg_id = _send_telegram_new(_step_initial_text, target=target)
        if step_msg_id and use_step_msg_file:
            smf.parent.mkdir(parents=True, exist_ok=True)
            smf.write_text(json.dumps({'msg_id': step_msg_id, 'text': _step_initial_text, 'stream_id': step_stream_id}))
    else:
        smf.unlink(missing_ok=True)

    def _persist_smf(text: str, *, new_id: int | None=None) -> None:
        if not use_step_msg_file:
            return
        mid = new_id if new_id is not None else step_msg_id
        if not mid:
            return
        smf.parent.mkdir(parents=True, exist_ok=True)
        smf.write_text(json.dumps({'msg_id': mid, 'text': text, 'stream_id': step_stream_id}))

    def _edit_step_msg(text: str, *, force: bool=False, fallback_send: bool=False, min_interval: float=3.0):
        nonlocal last_edit, step_msg_text, step_msg_id
        if not step_msg_id or not target:
            return
        now = time.time()
        if not force and now - last_edit < min_interval:
            return
        body = _truncate_telegram_text(text)
        ok = _edit_telegram_text(step_msg_id, body, target=target)
        if not ok and fallback_send:
            _log('step message edit failed; sending new Telegram message with final state')
            new_id = _send_telegram_new('[step: could not edit in-place]\n\n' + body, target=target)
            if new_id:
                step_msg_text = text
                _persist_smf(text, new_id=new_id)
                step_msg_id = new_id
                last_edit = now
            return
        if ok:
            step_msg_text = text
            _persist_smf(text)
            last_edit = now
    _last_activity = ['']
    _rollout_buf = ['']
    _heartbeat_stop = threading.Event()

    def _on_text(streaming: str):
        _operator_tick()
        _rollout_buf[0] = streaming
        elapsed_s = time.monotonic() - t0
        bubble = _format_step_live_bubble(elapsed_s, step_label, _redact_secrets(streaming), _last_activity[0] or '')
        _edit_step_msg(bubble, min_interval=1.0)

    def _on_activity(status: str):
        _operator_tick()
        _last_activity[0] = status
        elapsed_s = time.monotonic() - t0
        bubble = _format_step_live_bubble(elapsed_s, step_label, _redact_secrets(_rollout_buf[0]), status)
        _edit_step_msg(bubble, min_interval=1.2)

    def _heartbeat():
        while not _heartbeat_stop.wait(timeout=3.0):
            _operator_tick()
            elapsed_s = time.monotonic() - t0
            status = _last_activity[0] or 'working ...'
            bubble = _format_step_live_bubble(elapsed_s, step_label, _redact_secrets(_rollout_buf[0]), status if status != 'working ...' else '')
            _edit_step_msg(bubble, force=True)
    if existing_step_msg_id is not None and step_msg_id:
        _edit_step_msg(_format_step_live_bubble(0.0, step_label, '', '', placeholder='(starting…)'), force=True)
    run_dir = make_run_dir()
    log_file = run_dir / 'logs.txt'
    runtime_state._tls.log_fh = open(log_file, 'a', encoding='utf-8')
    success = False
    result: subprocess.CompletedProcess | None = None
    failure_summary = ''
    try:
        _log(f'run dir {run_dir}')
        preview = prompt[:200] + ('…' if len(prompt) > 200 else '')
        _log(f'prompt preview: {preview}')
        _log(f'agent step {agent_step}: executing' + (' [force]' if force_step else ''))
        threading.Thread(target=_heartbeat, daemon=True).start()
        try:
            result = run_agent(_claude_cmd(prompt), phase='agent_step', output_file=run_dir / 'output.txt', run_dir=run_dir, invocation_kind='loop_step', step_label=step_label, prompt_est_tokens=_approx_prompt_context_tokens(prompt), extra_env={runtime_state.ARBOS_STEP_STREAM_ID_ENV: step_stream_id}, on_text=_on_text, on_activity=_on_activity)
        except Exception as exc:
            failure_summary = _redact_secrets(f'{type(exc).__name__}: {exc}')[:800]
            _log(f'run_step: run_agent raised: {failure_summary}')
            return (False, failure_summary)
        rollout_text = _redact_secrets(extract_text(result))
        (run_dir / 'rollout.md').write_text(rollout_text)
        _log(f'rollout saved ({len(rollout_text)} chars)')
        elapsed = time.monotonic() - t0
        success = result.returncode == 0
        _log(f"step {('succeeded' if success else 'failed')} in {fmt_duration(elapsed)}")
        if not success and result is not None:
            failure_summary = _redact_secrets(_build_agent_failure_detail(result))[:800]
            _log('step failure detail:\n' + _redact_secrets(_build_agent_failure_detail(result))[:4000])
        else:
            failure_summary = ''
        return (success, failure_summary)
    finally:
        _heartbeat_stop.set()
        fh = getattr(runtime_state._tls, 'log_fh', None)
        if fh:
            fh.close()
            runtime_state._tls.log_fh = None
        try:
            rollout = ''
            if run_dir is not None and (run_dir / 'rollout.md').exists():
                rollout = (run_dir / 'rollout.md').read_text()
            status = 'done' if success else 'failed'
            goal_text = runtime_state.GOAL_FILE.read_text().strip() if runtime_state.GOAL_FILE.exists() else ''
            inbox_text = runtime_state.INBOX_FILE.read_text().strip() if runtime_state.INBOX_FILE.exists() else ''
            go_text = runtime_state.GO_FLAG_FILE.read_text().strip() if runtime_state.GO_FLAG_FILE.exists() else ''
            agent_text = ''
            if smf.exists():
                try:
                    state = json.loads(smf.read_text())
                    saved = state.get('text', '')
                    if saved != _step_initial_text and (not saved.startswith('Step ( ')) and (not saved.startswith(f'{step_label} (running)')):
                        agent_text = saved
                except (json.JSONDecodeError, KeyError):
                    pass
            elapsed_s = time.monotonic() - t0
            hdr = _step_response_header(elapsed_s, step_label)
            if not success and result is not None:
                hdr = f'{hdr} — FAILED (exit {result.returncode})'
            else:
                hdr = f'{hdr} — {status}'
            parts = [hdr]
            log_tail = ''
            log_path = run_dir / 'logs.txt' if run_dir is not None else None
            if log_path and log_path.exists():
                raw_log = log_path.read_text(errors='replace').strip()
                if raw_log:
                    log_tail = raw_log[-2500:] if len(raw_log) > 2500 else raw_log
            stdout_text = (result.stdout or '').strip() if result is not None else ''
            failure_detail = _redact_secrets(_build_agent_failure_detail(result)) if not success and result is not None else failure_summary or ''
            state_text = _sync_state_after_step(step_label=step_label, success=success, elapsed_s=elapsed_s, rollout_text=rollout, failure_detail=failure_detail)
            summary = _summarize_step_outcome(step_label=step_label, success=success, elapsed_s=elapsed_s, goal_text=goal_text, state_text=state_text, inbox_text=inbox_text, go_text=go_text, rollout_text=rollout, log_tail=log_tail, stdout_text=stdout_text, failure_detail=failure_detail)
            has_summary = bool(summary)
            if has_summary:
                parts.append(summary)
            elif agent_text:
                parts.append(agent_text)
            if not has_summary and (not success) and (result is not None):
                if stdout_text:
                    parts.append('--- model text (stdout) ---\n' + stdout_text[:2000])
                parts.append(failure_detail[:2800])
            elif not has_summary and rollout.strip():
                parts.append(rollout.strip()[:3500])
            if not has_summary and log_tail:
                parts.append('--- step log (tail) ---\n' + _redact_secrets(log_tail))
            final = _truncate_telegram_text('\n\n'.join(parts))
            _edit_step_msg(final, force=True, fallback_send=True)
            log_chat('bot', final[:1000])
            smf.unlink(missing_ok=True)
        except Exception as exc:
            _log(f'step message finalize failed: {str(exc)[:120]}')
            tgt = _step_update_target()
            if tgt:
                _send_telegram_text(f'{step_label}: finalize/crash in run_step: {str(exc)[:500]}', target=tgt)

def _agent_wait(gs: runtime_state.AgentState, timeout: float) -> None:
    """Wait up to timeout seconds or until gs.wake; refresh operator ticks during long sleeps."""
    if timeout <= 0:
        gs.wake.clear()
        return
    end = time.monotonic() + timeout
    while not gs.stop_event.is_set():
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        slice_sec = min(remaining, 10.0)
        if gs.wake.wait(timeout=slice_sec):
            break
        _operator_tick()
    gs.wake.clear()

def _agent_loop():
    """Run the agent loop. Exits when stop_event is set."""
    with runtime_state._agent_lock:
        gs = runtime_state._agent
    failures = 0
    while not gs.stop_event.is_set():
        if not runtime_state.GOAL_FILE.exists() or not runtime_state.GOAL_FILE.read_text().strip():
            if gs.goal_hash:
                _log(f'goal cleared after {gs.step_count} steps')
                gs.goal_hash = ''
                gs.step_count = 0
            with runtime_state._agent_lock:
                if gs.paused:
                    gs.paused = False
                    _save_agent()
            _operator_set('idle', f'waiting for {_path_for_display(runtime_state.GOAL_FILE)} content')
            _agent_wait(gs, 5.0)
            continue
        if not runtime_state.GO_FLAG_FILE.exists():
            _operator_set('paused', f'paused — no {_path_for_display(runtime_state.GO_FLAG_FILE)} (/resume or /loop to enable steps; GOAL.md unchanged)')
            with runtime_state._agent_lock:
                if not gs.paused:
                    gs.paused = True
                    _save_agent()
            _agent_wait(gs, 5.0)
            continue
        with runtime_state._agent_lock:
            if gs.paused:
                gs.paused = False
                _save_agent()
        current_goal = runtime_state.GOAL_FILE.read_text().strip()
        current_hash = hashlib.sha256(current_goal.encode()).hexdigest()[:16]
        if current_hash != gs.goal_hash:
            if gs.goal_hash:
                _log(f'goal changed after {gs.step_count} steps on previous text')
            gs.goal_hash = current_hash
            gs.step_count = 0
            _log(f'goal new [{current_hash}]: {current_goal[:100]}')
        runtime_state._step_count += 1
        gs.step_count += 1
        gs.last_run = datetime.now().isoformat()
        with runtime_state._agent_lock:
            _save_agent()
        _log(f'Loop step {gs.step_count} (global step {runtime_state._step_count})', blank=True)
        with runtime_state._pending_step_msg_lock:
            pre_id = runtime_state._pending_step_msg_id
            runtime_state._pending_step_msg_id = None
        if pre_id is None:
            pre_id = _pre_send_normal_step_bubble(gs.step_count, runtime_state._step_count)
        prompt = load_prompt(consume_inbox=True, agent_step=gs.step_count)
        if not prompt:
            _operator_set('idle', 'empty prompt; waiting')
            if pre_id:
                tgt = _step_update_target()
                if tgt:
                    _edit_telegram_text(pre_id, _truncate_telegram_text('Step skipped: empty prompt. Add GOAL/STATE or use /loop.'), target=tgt)
                runtime_state.STEP_MSG_FILE.unlink(missing_ok=True)
            _agent_wait(gs, 5.0)
            continue
        _log(f'agent: prompt={len(prompt)} chars')
        _operator_set('agent_step', f'step {gs.step_count} — Claude running')
        (success, failure_summary) = run_step(prompt, runtime_state._step_count, agent_step=gs.step_count, existing_step_msg_id=pre_id)
        gs.last_finished = datetime.now().isoformat()
        with runtime_state._agent_lock:
            _save_agent()
        if success:
            failures = 0
            gs.last_step_ok = True
            gs.last_step_error = ''
            _operator_set('between_steps', f'step {gs.step_count} finished OK', last_error='')
        else:
            failures += 1
            gs.last_step_ok = False
            gs.last_step_error = (failure_summary or 'step failed (no detail)')[:1200]
            _log(f'agent: failure #{failures}')
        gs.wake.clear()
        delay_secs = gs.delay_minutes * 60 + int(os.environ.get('AGENT_DELAY', '0'))
        if failures:
            backoff = min(2 ** failures, 120)
            delay_secs += backoff
            _log(f'agent: waiting {delay_secs}s (failure backoff + delay)')
            _operator_set('between_steps', f'waiting {delay_secs}s (backoff + delay) then next step', last_error=gs.last_step_error)
            _agent_wait(gs, float(delay_secs))
        elif delay_secs > 0:
            _log(f'agent: waiting {delay_secs}s (delay)')
            _operator_set('between_steps', f'waiting {delay_secs}s before next step')
            _agent_wait(gs, float(delay_secs))
        else:
            _operator_tick()
    _log('agent loop exited')

def _ensure_agent_thread() -> None:
    """Spawn the agent loop thread if started but not running; clear stale dead threads.

    Call after /loop or /resume so we do not wait for the 2s _agent_manager poll.
    """
    with runtime_state._agent_lock:
        gs = runtime_state._agent
        if gs.thread is not None and (not gs.thread.is_alive()):
            gs.thread = None
        if gs.started and gs.thread is None:
            gs.stop_event.clear()
            t = threading.Thread(target=_agent_loop, daemon=True, name='agent')
            gs.thread = t
            t.start()
            _log('agent thread spawned (ensure)')

def _agent_manager():
    """Spawn or stop the single agent thread based on AgentState."""
    while not runtime_state._shutdown.is_set():
        with runtime_state._agent_lock:
            gs = runtime_state._agent
            if not gs.started and gs.thread is not None:
                gs.stop_event.set()
                gs.wake.set()
        _ensure_agent_thread()
        runtime_state._shutdown.wait(timeout=2)

def _kill_child_procs():
    """Kill all tracked claude child processes."""
    with runtime_state._child_procs_lock:
        procs = list(runtime_state._child_procs)
    for proc in procs:
        try:
            if proc.poll() is None:
                _log(f'killing child claude pid={proc.pid}')
                _mark_claude_invocation_pid_status(proc.pid, status='killed', detail='killed by Arbos supervisor')
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            pass
    with runtime_state._child_procs_lock:
        runtime_state._child_procs.clear()

def _pid_looks_like_claude(pid: int) -> bool:
    """Best-effort guard against killing a reused, unrelated PID."""
    if pid <= 0:
        return False
    proc_dir = Path('/proc') / str(pid)
    try:
        cmdline = (proc_dir / 'cmdline').read_bytes().replace(b'\x00', b' ').decode('utf-8', 'ignore')
        if cmdline.strip():
            return 'claude' in cmdline.lower()
    except OSError:
        pass
    try:
        return 'claude' in (proc_dir / 'comm').read_text().strip().lower()
    except OSError:
        return False

def _kill_registered_claude_procs(*, detail: str) -> int:
    """Kill running Claude PIDs from the in-memory + on-disk registry."""
    pids: set[int] = set()
    with runtime_state._claude_invocations_lock:
        for meta in runtime_state._claude_invocations.values():
            if meta.get('status') != 'running':
                continue
            pid = _safe_int(meta.get('pid'))
            if pid:
                pids.add(pid)
    if runtime_state.CLAUDE_INVOCATIONS_FILE.exists():
        try:
            payload = json.loads(runtime_state.CLAUDE_INVOCATIONS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            payload = {}
        for item in payload.get('items', []):
            if not isinstance(item, dict) or item.get('status') != 'running':
                continue
            pid = _safe_int(item.get('pid'))
            if pid:
                pids.add(pid)
    killed = 0
    for pid in sorted(pids):
        if pid == os.getpid() or not _pid_looks_like_claude(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
            _log(f'killed registered claude pid={pid}')
        except ProcessLookupError:
            pass
        except PermissionError:
            continue
        finally:
            _mark_claude_invocation_pid_status(pid, status='killed', detail=detail)
    return killed

def _truncate_context_logs() -> None:
    import shutil
    if not runtime_state.CONTEXT_LOGS_DIR.exists():
        runtime_state.CONTEXT_LOGS_DIR.mkdir(parents=True, exist_ok=True)
        return
    for path in runtime_state.CONTEXT_LOGS_DIR.iterdir():
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            continue
        try:
            path.write_text('')
        except OSError:
            pass
    runtime_state.CONTEXT_LOGS_DIR.mkdir(parents=True, exist_ok=True)

def _clear_agent_runtime_history() -> None:
    """Remove persisted state/history so the next run starts clean."""
    import shutil
    runtime_state.STEP_MSG_FILE.unlink(missing_ok=True)
    for path in (runtime_state.GOAL_FILE, runtime_state.GO_FLAG_FILE, runtime_state.STATE_FILE, runtime_state.INBOX_FILE, runtime_state.META_FILE):
        path.unlink(missing_ok=True)
    if runtime_state.RUNS_DIR.exists():
        shutil.rmtree(runtime_state.RUNS_DIR, ignore_errors=True)
    runtime_state.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    if runtime_state.CHATLOG_DIR.exists():
        shutil.rmtree(runtime_state.CHATLOG_DIR, ignore_errors=True)
    runtime_state.CHATLOG_DIR.mkdir(parents=True, exist_ok=True)
    _reset_claude_invocations()
    _truncate_context_logs()

def _kill_stale_claude_procs():
    """Kill leftover Claude subprocesses recorded for this project only."""
    if not runtime_state.CLAUDE_INVOCATIONS_FILE.exists():
        return
    try:
        payload = json.loads(runtime_state.CLAUDE_INVOCATIONS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return
    for item in payload.get('items', []):
        if item.get('status') != 'running':
            continue
        pid = int(item.get('pid') or 0)
        if not pid or pid == os.getpid():
            continue
        if not _pid_looks_like_claude(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            _log(f'killed stale claude pid={pid} for project {runtime_state.PROJECT_NAME}')
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
