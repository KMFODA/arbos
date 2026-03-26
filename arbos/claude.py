from __future__ import annotations
import json
import os
import selectors
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
import requests
from . import runtime as runtime_state
from .env import _redact_secrets
from .logs import _approx_prompt_context_tokens, _arbos_response_header, _format_tool_activity, _log, _mark_prompt_context_attempt_started, _parse_result_usage, _record_prompt_context_usage, _reset_prompt_context_usage, fmt_duration
from .runtime import _operator_set, _operator_tick
from .state import _finalize_claude_invocation, _mark_claude_invocation_pid_status, _register_claude_invocation, _summarize_claude_cmd, make_run_dir
from .telegram import TELEGRAM_TEXT_MAX, _streaming_empty_summary, _tail_text_for_telegram, _telegram_bot_edit_message_text, _telegram_bot_send_message, _telegram_send_message_fallback, _truncate_telegram_text

def _claude_cmd(prompt: str, extra_flags: list[str] | None=None) -> list[str]:
    cmd = ['claude', '-p', prompt]
    if not runtime_state.IS_ROOT:
        cmd.append('--dangerously-skip-permissions')
    cmd.extend(['--output-format', 'stream-json', '--verbose'])
    if extra_flags:
        cmd.extend(extra_flags)
    return cmd

def _write_claude_settings():
    """Point Claude Code at OpenRouter (Anthropic-compatible API)."""
    settings_dir = runtime_state.CLAUDE_SETTINGS_DIR
    settings_dir.mkdir(parents=True, exist_ok=True)
    env_block = {'ANTHROPIC_API_KEY': runtime_state.LLM_API_KEY, 'ANTHROPIC_BASE_URL': runtime_state.LLM_BASE_URL, 'ANTHROPIC_AUTH_TOKEN': '', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}
    settings = {'model': runtime_state.CLAUDE_MODEL, 'permissions': {'allow': ['Bash(*)', 'Read(*)', 'Write(*)', 'Edit(*)', 'Glob(*)', 'Grep(*)', 'WebFetch(*)', 'WebSearch(*)', 'TodoWrite(*)', 'NotebookEdit(*)', 'Task(*)']}, 'env': env_block}
    (settings_dir / 'settings.local.json').write_text(json.dumps(settings, indent=2))
    _log(f"wrote {settings_dir / 'settings.local.json'} (openrouter, model={runtime_state.CLAUDE_MODEL}, target={runtime_state.LLM_BASE_URL})")

def _claude_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop('TAU_BOT_TOKEN', None)
    env['ANTHROPIC_API_KEY'] = runtime_state.LLM_API_KEY
    env['ANTHROPIC_BASE_URL'] = runtime_state.LLM_BASE_URL
    env['ANTHROPIC_AUTH_TOKEN'] = ''
    env['ARBOS_PROJECT'] = runtime_state.PROJECT_NAME
    env['ARBOS_PROJECT_DIR'] = str(runtime_state.PROJECT_DIR)
    env['ARBOS_WORKSPACE_DIR'] = str(runtime_state.WORKSPACE_DIR)
    return env

def _join_stream_text_chunks(parts: list[str]) -> str:
    """Join Claude stream-json text deltas without gluing sentences together.

    Sequential deltas often omit whitespace at boundaries (e.g. ``...tests.`` +
    ``Building...``). Insert a newline when sentence-ending punctuation meets a
    capital letter so context logs and Telegram stay readable; this avoids
    splitting inside words (``Build`` + ``ing``).
    """
    out: list[str] = []
    for t in parts:
        if not t:
            continue
        if out:
            prev = out[-1]
            if prev and t and (not prev[-1].isspace()) and (not t[0].isspace()) and (prev[-1] in '.!?') and t[0].isupper():
                out.append('\n')
        out.append(t)
    return ''.join(out)

def _prefer_richer_assistant_text(result_text: str, complete_texts: list[str], streaming_tokens: list[str]) -> str:
    """Pick assistant text to show after the CLI exits.

    The final stream-json ``result`` field is sometimes a short post-tool phrase
    (for example after sending a message) while the substantive answer was
    already streamed. Prefer the substantially longer assistant payload so the
    operator keeps the full reply.
    """
    r = (result_text or '').strip()
    joined_complete = '\n\n'.join(complete_texts).strip()
    streamed = _join_stream_text_chunks(streaming_tokens).strip()
    candidates = [x for x in (joined_complete, streamed, r) if x]
    if not candidates:
        return result_text or ''
    longest = max(candidates, key=len)
    if len(longest) > len(r) + 80:
        return longest
    return r if r else longest

def _run_claude_once(cmd, env, on_text=None, on_activity=None, *, invocation_meta: dict[str, Any] | None=None):
    """Run a single claude subprocess, return (returncode, result_text, raw_lines, stderr, usage).

    on_text: optional callback(accumulated_text) fired as assistant text streams in.
    on_activity: optional callback(status_str) fired on tool use and other activity.
    If CLAUDE_IDLE_KILL is true, kills the process after CLAUDE_TIMEOUT seconds with no
    stdout or stderr activity. Set CLAUDE_TIMEOUT=0 to disable that watchdog (still exits
    when the process ends). Stderr is drained continuously so a chatty CLI cannot deadlock
    on a full PIPE buffer.
    """
    proc = subprocess.Popen(cmd, cwd=runtime_state.PROJECT_DIR, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    with runtime_state._child_procs_lock:
        runtime_state._child_procs.add(proc)
    invocation_id = None
    if invocation_meta is not None:
        invocation_id = _register_claude_invocation(proc, cmd=cmd, **invocation_meta)
    result_text = ''
    complete_texts: list[str] = []
    streaming_tokens: list[str] = []
    raw_lines: list[str] = []
    stderr_acc: list[str] = []
    timed_out = False
    last_activity = time.monotonic()
    stdout_registered = True
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    sel.register(proc.stderr, selectors.EVENT_READ)

    def _consume_stdout_line(line: str) -> None:
        nonlocal result_text
        raw_lines.append(line)
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            return
        etype = evt.get('type', '')
        if etype == 'assistant':
            msg = evt.get('message', {})
            for block in msg.get('content', []):
                btype = block.get('type', '')
                if btype == 'text' and block.get('text'):
                    if evt.get('model_call_id'):
                        complete_texts.append(block['text'])
                        streaming_tokens.clear()
                    else:
                        streaming_tokens.append(block['text'])
                        if on_text:
                            on_text(_join_stream_text_chunks(streaming_tokens))
                elif btype == 'tool_use' and on_activity:
                    tool_name = block.get('name', '')
                    tool_input = block.get('input', {})
                    on_activity(_format_tool_activity(tool_name, tool_input))
        elif etype == 'item.completed':
            item = evt.get('item', {})
            if item.get('type') == 'agent_message' and item.get('text'):
                complete_texts.append(item['text'])
                streaming_tokens.clear()
                if on_text:
                    on_text(item['text'])
        elif etype == 'result':
            result_text = evt.get('result', '')
    try:
        while True:
            select_timeout = min(runtime_state.CLAUDE_TIMEOUT, 30) if runtime_state.CLAUDE_IDLE_KILL else 30.0
            ready = sel.select(timeout=select_timeout)
            if not ready:
                if runtime_state.CLAUDE_IDLE_KILL and time.monotonic() - last_activity > runtime_state.CLAUDE_TIMEOUT:
                    _log(f'claude timeout: no stdout/stderr activity for {runtime_state.CLAUDE_TIMEOUT}s, killing pid={proc.pid}')
                    _mark_claude_invocation_pid_status(proc.pid, status='timed_out', detail=f'timed out after {runtime_state.CLAUDE_TIMEOUT}s idle')
                    proc.kill()
                    timed_out = True
                    break
                if proc.poll() is not None:
                    break
                continue
            for (key, _) in ready:
                if key.fileobj == proc.stdout:
                    line = proc.stdout.readline()
                    if not line:
                        if stdout_registered:
                            try:
                                sel.unregister(proc.stdout)
                            except (KeyError, ValueError):
                                pass
                            stdout_registered = False
                    else:
                        last_activity = time.monotonic()
                        _consume_stdout_line(line)
                elif key.fileobj == proc.stderr:
                    err_line = proc.stderr.readline()
                    if err_line:
                        last_activity = time.monotonic()
                        stderr_acc.append(err_line)
            if not stdout_registered and proc.poll() is not None:
                break
    finally:
        for fo in (proc.stdout, proc.stderr):
            try:
                sel.unregister(fo)
            except (KeyError, ValueError):
                pass
        sel.close()
    if proc.stdout:
        try:
            rest = proc.stdout.read()
            if rest:
                for line in rest.splitlines(keepends=True):
                    _consume_stdout_line(line)
        except Exception:
            pass
    if proc.stderr:
        try:
            rest = proc.stderr.read()
            if rest:
                stderr_acc.append(rest)
        except Exception:
            pass
    if not result_text:
        if complete_texts:
            result_text = complete_texts[-1]
        elif streaming_tokens:
            result_text = _join_stream_text_chunks(streaming_tokens)
    result_text = _prefer_richer_assistant_text(result_text, complete_texts, streaming_tokens)
    stderr_output = ''.join(stderr_acc)
    if timed_out:
        stderr_output = f'(timed out after {runtime_state.CLAUDE_TIMEOUT}s idle)\n{stderr_output}'.strip()
    returncode = proc.wait()
    with runtime_state._child_procs_lock:
        runtime_state._child_procs.discard(proc)
    usage = _parse_result_usage(raw_lines)
    _finalize_claude_invocation(invocation_id, returncode=returncode, stderr_output=stderr_output, usage=usage, status='timed_out' if timed_out else None)
    return (returncode, result_text, raw_lines, stderr_output, usage)

def run_agent(cmd: list[str], phase: str, output_file: Path, *, run_dir: Path | None=None, invocation_kind: str='loop_step', step_label: str | None=None, prompt_est_tokens: int=0, extra_env: dict[str, str] | None=None, on_text=None, on_activity=None) -> subprocess.CompletedProcess:
    env = _claude_env()
    if extra_env:
        env.update(extra_env)
    flags = ' '.join((a for a in cmd if a.startswith('-')))
    (returncode, result_text, raw_lines, stderr_output) = (1, '', [], 'no attempts made')
    for attempt in range(1, runtime_state.MAX_RETRIES + 1):
        _log(f'{phase}: starting (attempt={attempt}) flags=[{flags}]')
        _mark_prompt_context_attempt_started(prompt_est_tokens)
        t0 = time.monotonic()
        (returncode, result_text, raw_lines, stderr_output, usage) = _run_claude_once(cmd, env, on_text=on_text, on_activity=on_activity, invocation_meta={'kind': invocation_kind, 'phase': phase, 'run_dir': run_dir, 'attempt': attempt, 'prompt_est_tokens': prompt_est_tokens, 'step_label': step_label})
        paid_usage = _record_prompt_context_usage(usage)
        elapsed = time.monotonic() - t0
        output_file.write_text(_redact_secrets(''.join(raw_lines)))
        _log(f'{phase}: finished rc={returncode} {fmt_duration(elapsed)}')
        if usage is not None:
            _log(f'{phase}: usage in={usage.total_input_tokens} (direct={usage.input_tokens} cache_write={usage.cache_creation_input_tokens} cache_read={usage.cache_read_input_tokens}) out={usage.output_tokens}')
            _log(f'{phase}: rollout paid total in={paid_usage.total_input_tokens} out={paid_usage.output_tokens}')
        if returncode != 0:
            if stderr_output.strip():
                _log(f'{phase}: stderr {stderr_output.strip()[:300]}')
            else:
                _log(f'{phase}: nonzero exit with empty stderr')
            if attempt < runtime_state.MAX_RETRIES:
                delay = min(2 ** attempt, 30)
                _log(f'{phase}: retrying in {delay}s (attempt {attempt}/{runtime_state.MAX_RETRIES})')
                time.sleep(delay)
                continue
        return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout=result_text, stderr=stderr_output)
    _log(f'{phase}: all {runtime_state.MAX_RETRIES} retries exhausted')
    output_file.write_text(_redact_secrets(''.join(raw_lines)))
    return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout=result_text, stderr=stderr_output)

def extract_text(result: subprocess.CompletedProcess) -> str:
    out = (result.stdout or '').strip()
    if out:
        return result.stdout or ''
    err = (result.stderr or '').strip()
    if err:
        return result.stderr or ''
    return f'(no stdout/stderr from agent; exit_code={result.returncode})'

def run_agent_streaming(bot, prompt: str, chat_id: int, *, reply_to_message_id: int | None=None, message_thread_id: int | None=None) -> str:
    """Run Claude Code CLI and stream output into Telegram.

    The *active* segment is updated with ``editMessageText``. When the CLI
    starts a new assistant segment (non-prefix text), the previous segment is
    left frozen in its bubble and a new message is sent so the chat shows the
    full rollout (each bubble up to Telegram's size limit).

    When ``reply_to_message_id`` is set, new Telegram messages (including the
    initial status bubble) are sent as replies to that operator message.

    When ``message_thread_id`` is set (forum supergroup topic), it is passed on
    every ``sendMessage`` so standalone operator messages appear in the same
    topic. Without it, Telegram posts to the General topic while replies would
    still land in the topic of the quoted message.
    """
    cmd = _claude_cmd(prompt)
    t0 = time.monotonic()
    _reset_prompt_context_usage(prompt)

    def _elapsed() -> float:
        return time.monotonic() - t0

    def _format_display(core: str) -> str:
        core = (core or '').strip() or '…'
        return _truncate_telegram_text(f'{_arbos_response_header(_elapsed())}\n\n{core}')
    _reply_kw: dict[str, Any] = {}
    if reply_to_message_id is not None:
        _reply_kw['reply_to_message_id'] = reply_to_message_id
    if message_thread_id is not None:
        _reply_kw['message_thread_id'] = message_thread_id
    current_text = ''
    _start_core = f'Starting Claude… (attempt 1/{runtime_state.MAX_RETRIES})'
    _phase_line = _start_core
    _segment_done: list[str] = []
    _stream_seg: str = ''
    _stream_tail: str = ''

    def _stream_core() -> str:
        chunks = []
        if _stream_seg.strip():
            chunks.append(_stream_seg.strip())
        if _stream_tail.strip():
            chunks.append(_stream_tail.strip())
        return '\n\n'.join(chunks) if chunks else ''

    def _display_core() -> str:
        core = _stream_core()
        if core:
            return core
        return (_phase_line or '').strip() or '…'

    def _final_transcript() -> str:
        chunks = [s.strip() for s in _segment_done if s.strip()]
        if _stream_seg.strip():
            chunks.append(_stream_seg.strip())
        return '\n\n'.join(chunks) if chunks else ''
    try:
        (msg, used_kw) = _telegram_send_message_fallback(bot, chat_id, _format_display(_start_core), _reply_kw)
        if used_kw != _reply_kw:
            _log(f"run_agent_streaming: initial send used relaxed Telegram kwargs {list(used_kw.keys()) or '(none)'}")
    except Exception as exc:
        _log(f'run_agent_streaming: initial send_message failed: {str(exc)[:250]}')
        notice = f'Arbos could not open a status message on Telegram.\n\n{exc}\n\nYour text was still received. If this repeats, check DNS/network to api.telegram.org.'
        try:
            _telegram_send_message_fallback(bot, chat_id, notice, {})
        except Exception as exc2:
            _log(f'run_agent_streaming: could not notify chat of Telegram failure: {str(exc2)[:220]}')
        return f'(could not post operator status to Telegram: {exc})'
    run_dir = make_run_dir()
    runtime_state._tls.log_fh = open(run_dir / 'logs.txt', 'a', encoding='utf-8')
    _log(f'operator run dir {run_dir}')
    _pp = _redact_secrets(prompt[:200] + ('…' if len(prompt) > 200 else ''))
    _log(f'operator prompt preview: {_pp}')
    _rto = reply_to_message_id
    _tid = message_thread_id
    _log(f'operator meta: model={runtime_state.CLAUDE_MODEL} chat_id={chat_id}' + (f' reply_to_message_id={_rto}' if _rto is not None else '') + (f' message_thread_id={_tid}' if _tid is not None else ''))
    last_edit = 0.0
    _heartbeat_stop = threading.Event()
    last_raw_lines: list[str] = []
    last_attempt = 1
    (last_rc, last_stderr) = (0, '')

    def _stream_heartbeat():
        while not _heartbeat_stop.wait(timeout=3.0):
            _operator_tick()
            _paint(force=True, refresh_only=True)

    def _paint(force: bool=False, *, refresh_only: bool=False, send_if_edit_fails: bool=False):
        nonlocal last_edit, msg
        now = time.time()
        if not force and (not refresh_only) and (now - last_edit < 1.5):
            return
        display = _redact_secrets(_format_display(_display_core()))
        if not display.strip():
            return
        try:
            if _telegram_bot_edit_message_text(bot, display, chat_id, msg.message_id):
                last_edit = now
        except Exception as exc:
            _log(f'run_agent_streaming: edit_message_text failed: {str(exc)[:220]}')
            if send_if_edit_fails:
                try:
                    sent = _telegram_bot_send_message(bot, chat_id, display[:TELEGRAM_TEXT_MAX], **_reply_kw)
                    if sent is not None:
                        _log('run_agent_streaming: sent fallback new message after edit failure')
                except Exception as exc2:
                    _log(f'run_agent_streaming: fallback send_message failed: {str(exc2)[:220]}')

    def _freeze_segment_and_new_message(completed: str, new_seg_raw: str) -> None:
        """Leave *completed* in the current bubble; open a new message for *new_seg_raw*."""
        nonlocal msg
        c = (completed or '').strip()
        if c:
            body = _redact_secrets(_format_display(c))
            try:
                _telegram_bot_edit_message_text(bot, body, chat_id, msg.message_id)
            except Exception as exc:
                _log(f'run_agent_streaming: freeze-segment edit failed: {str(exc)[:220]}')
                try:
                    sent = _telegram_bot_send_message(bot, chat_id, body[:TELEGRAM_TEXT_MAX], **_reply_kw)
                    if sent is not None:
                        _log('run_agent_streaming: freeze segment sent as new message after edit fail')
                except Exception as exc2:
                    _log(f'run_agent_streaming: freeze segment send_message failed: {str(exc2)[:220]}')
            _segment_done.append(c)
        core_new = (new_seg_raw or '').strip() or '…'
        try:
            new_msg = _telegram_bot_send_message(bot, chat_id, _redact_secrets(_format_display(core_new)), **_reply_kw)
            if new_msg is not None:
                msg = new_msg
        except Exception as exc:
            _log(f'run_agent_streaming: new-segment send_message failed: {str(exc)[:250]}')

    def _on_text(text: str):
        nonlocal _stream_seg, _stream_tail
        t = text or ''
        pt = _stream_seg.strip()
        tt = t.strip()
        _stream_tail = ''
        if not pt:
            _stream_seg = t
        elif tt.startswith(pt):
            _stream_seg = t
        else:
            _freeze_segment_and_new_message(pt, t)
            _stream_seg = t
        _paint()

    def _on_activity(status: str):
        nonlocal _stream_tail
        _operator_tick()
        _stream_tail = (status or '').strip()
        _paint()
    with runtime_state._operator_lock:
        _prev_operator_phase = runtime_state._operator['phase']
        _prev_operator_detail = runtime_state._operator['detail']
    _operator_set('operator_chat', 'Telegram /operator — Claude streaming')
    try:
        threading.Thread(target=_stream_heartbeat, daemon=True, name='operator-stream-hb').start()
        env = _claude_env()
        for attempt in range(1, runtime_state.MAX_RETRIES + 1):
            last_attempt = attempt
            current_text = ''
            last_edit = 0.0
            _segment_done.clear()
            _stream_seg = ''
            _stream_tail = ''
            _phase_line = 'Thinking ...'
            _paint(force=True)
            _log(f'run_agent_streaming: attempt {attempt}/{runtime_state.MAX_RETRIES} starting')
            _mark_prompt_context_attempt_started(_approx_prompt_context_tokens(prompt))
            t_attempt = time.monotonic()
            (returncode, result_text, raw_lines, stderr_output, usage) = _run_claude_once(cmd, env, on_text=_on_text, on_activity=_on_activity, invocation_meta={'kind': 'operator_chat', 'phase': 'operator_chat', 'run_dir': run_dir, 'attempt': attempt, 'prompt_est_tokens': _approx_prompt_context_tokens(prompt), 'step_label': 'operator chat'})
            last_raw_lines = raw_lines
            paid_usage = _record_prompt_context_usage(usage)
            (last_rc, last_stderr) = (returncode, stderr_output)
            attempt_s = time.monotonic() - t_attempt
            _log(f"run_agent_streaming: attempt {attempt} finished rc={returncode} duration_s={attempt_s:.2f} raw_lines={len(raw_lines)} text_len={len(result_text or '')} stderr_len={len(stderr_output or '')}")
            if usage is not None:
                _log(f'run_agent_streaming: usage in={usage.total_input_tokens} (direct={usage.input_tokens} cache_write={usage.cache_creation_input_tokens} cache_read={usage.cache_read_input_tokens}) out={usage.output_tokens}')
                _log(f'run_agent_streaming: rollout paid total in={paid_usage.total_input_tokens} out={paid_usage.output_tokens}')
            _se = (stderr_output or '').strip()
            if _se:
                _log(f'run_agent_streaming: attempt {attempt} stderr preview: {_redact_secrets(_se)[:800]}')
            elif returncode != 0:
                _log(f'run_agent_streaming: attempt {attempt} rc={returncode} with empty stderr')
            tr = _final_transcript().strip()
            rt = (result_text or '').strip()
            if tr or rt:
                if tr:
                    current_text = _final_transcript()
                else:
                    current_text = rt
                    _stream_tail = ''
                    _stream_seg = result_text or rt
                break
            if attempt < runtime_state.MAX_RETRIES:
                delay = min(2 ** attempt, 30)
                detail = _streaming_empty_summary(returncode, stderr_output, attempt)
                _segment_done.clear()
                _stream_seg = ''
                _stream_tail = ''
                _phase_line = f'{detail}\n\nRetrying in {delay}s (next {attempt + 1}/{runtime_state.MAX_RETRIES})…'
                _paint(force=True)
                time.sleep(delay)
                continue
            current_text = _streaming_empty_summary(returncode, stderr_output, attempt)
            _segment_done.clear()
            _stream_seg = ''
            _stream_tail = ''
            _phase_line = current_text
            break
        _paint(force=True, send_if_edit_fails=True)
        if not current_text.strip():
            fallback = _streaming_empty_summary(last_rc, last_stderr, last_attempt)
            _log(f'run_agent_streaming: final still empty; pushing diagnostic len={len(fallback)}')
            try:
                _telegram_bot_send_message(bot, chat_id, _redact_secrets(_format_display(fallback))[:TELEGRAM_TEXT_MAX], **_reply_kw)
            except Exception as exc:
                _log(f'run_agent_streaming: could not send final diagnostic: {str(exc)[:200]}')
    except Exception as e:
        current_text = f'(operator failed: {type(e).__name__}: {e})'
        _log(f'run_agent_streaming: exception: {str(e)[:500]}')
        _operator_set('operator_chat_error', 'Telegram operator run crashed', last_error=f'{type(e).__name__}: {e}'[:800])
        err_body = _truncate_telegram_text(f'{_arbos_response_header(_elapsed())}\n\nArbos error (operator run):\n{type(e).__name__}: {e}')
        try:
            _telegram_bot_edit_message_text(bot, _redact_secrets(err_body), chat_id, msg.message_id)
        except Exception as exc:
            _log(f'run_agent_streaming: could not edit with error text: {str(exc)[:200]}')
            try:
                _telegram_bot_send_message(bot, chat_id, _redact_secrets(err_body)[:TELEGRAM_TEXT_MAX], **_reply_kw)
            except Exception as exc2:
                _log(f'run_agent_streaming: could not send error message: {str(exc2)[:200]}')
    finally:
        try:
            (run_dir / 'output.txt').write_text(_redact_secrets(''.join(last_raw_lines)))
            rbody = (current_text or '').strip()
            if not rbody:
                rbody = _redact_secrets(_streaming_empty_summary(last_rc, last_stderr, last_attempt))
            else:
                rbody = _redact_secrets(rbody)
            (run_dir / 'rollout.md').write_text(rbody)
            _log(f'operator rollout saved ({len(rbody)} chars) total_elapsed={fmt_duration(_elapsed())} last_rc={last_rc} attempts_used={last_attempt}')
        except Exception as exc:
            _log(f'operator run artifact save failed: {str(exc)[:120]}')
        _heartbeat_stop.set()
        with runtime_state._operator_lock:
            runtime_state._operator['phase'] = _prev_operator_phase
            runtime_state._operator['detail'] = _prev_operator_detail
            runtime_state._operator['last_tick_wall'] = time.time()
        fh = getattr(runtime_state._tls, 'log_fh', None)
        if fh:
            try:
                fh.close()
            except OSError:
                pass
            runtime_state._tls.log_fh = None
    return current_text

def _summarize_goal(text: str) -> str:
    """Generate a one-line summary of a goal via OpenRouter. Falls back to truncation."""
    try:
        url = f'{runtime_state.LLM_BASE_URL}/v1/chat/completions'
        headers = {'Authorization': f'Bearer {runtime_state.LLM_API_KEY}', 'Content-Type': 'application/json'}
        resp = requests.post(url, json={'model': runtime_state.CLAUDE_MODEL, 'max_tokens': 50, 'messages': [{'role': 'system', 'content': "Summarize the user's goal in 8 words or fewer. Reply with ONLY the summary."}, {'role': 'user', 'content': text[:500]}]}, headers=headers, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            choices = data.get('choices', [])
            if choices:
                summary = choices[0].get('message', {}).get('content', '').strip().strip('"\'.')
                if summary:
                    return summary[:80]
    except Exception as exc:
        _log(f'summarize failed: {str(exc)[:100]}')
    first_line = text[:60].split('\n')[0].strip()
    return first_line + ('...' if len(text) > 60 else '')

def _openrouter_chat_text(messages: list[dict[str, str]], *, max_tokens: int, timeout: int=45) -> str:
    """Send a direct OpenRouter chat request and return the text reply."""
    if not runtime_state.LLM_API_KEY:
        return ''
    try:
        response = requests.post(f'{runtime_state.LLM_BASE_URL}/v1/chat/completions', json={'model': runtime_state.CLAUDE_MODEL, 'max_tokens': max_tokens, 'messages': messages}, headers={'Authorization': f'Bearer {runtime_state.LLM_API_KEY}', 'Content-Type': 'application/json'}, timeout=timeout)
        if response.status_code != 200:
            _log(f'openrouter chat failed: status={response.status_code} body={response.text[:200]}')
            return ''
        data = response.json()
        choices = data.get('choices', [])
        if not choices:
            return ''
        content = choices[0].get('message', {}).get('content', '')
        return _redact_secrets((content or '').strip())
    except Exception as exc:
        _log(f'openrouter chat failed: {str(exc)[:120]}')
        return ''

def _clip_summary_context(text: str, max_chars: int) -> str:
    text = (text or '').strip()
    if not text:
        return ''
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + '\n...[truncated]...'

def _summarize_step_outcome(*, step_label: str, success: bool, elapsed_s: float, goal_text: str, state_text: str, inbox_text: str, go_text: str, rollout_text: str, log_tail: str, stdout_text: str='', failure_detail: str='') -> str:
    """Create a concise operator-facing summary of the completed step."""
    status = 'success' if success else 'failure'
    _log(f'step summary: requesting OpenRouter summary for {step_label} ({status})')
    packet_parts = [f'Step label: {step_label}', f'Status: {status}', f'Elapsed seconds: {elapsed_s:.1f}']
    if goal_text.strip():
        packet_parts.append('## GOAL.md\n' + _clip_summary_context(goal_text, 2500))
    if state_text.strip():
        packet_parts.append('## STATE.md\n' + _clip_summary_context(state_text, 2500))
    if inbox_text.strip():
        packet_parts.append('## INBOX.md\n' + _clip_summary_context(inbox_text, 1200))
    if go_text.strip():
        packet_parts.append('## GO.md\n' + _clip_summary_context(go_text, 600))
    if rollout_text.strip():
        packet_parts.append('## rollout.md\n' + _clip_summary_context(rollout_text, 5000))
    if stdout_text.strip():
        packet_parts.append('## stdout\n' + _clip_summary_context(stdout_text, 2000))
    if failure_detail.strip():
        packet_parts.append('## failure detail\n' + _clip_summary_context(failure_detail, 3000))
    if log_tail.strip():
        packet_parts.append('## logs tail\n' + _clip_summary_context(log_tail, 3500))
    user_packet = _redact_secrets('\n\n'.join(packet_parts))
    summary = _openrouter_chat_text([{'role': 'system', 'content': 'You are summarizing a just-finished Arbos loop step for the operator. Summarize only what actually happened from the supplied artifacts. Be concrete about files or state changes, mention failures or blockers if present, and include the most important next action only if it is clearly implied. Reply in plain text, concise, no markdown headings, at most 6 short lines.'}, {'role': 'user', 'content': user_packet}], max_tokens=220)
    if summary:
        _log(f'step summary: received {len(summary)} chars for {step_label}')
    else:
        _log(f'step summary: no summary returned for {step_label}')
    return summary

def transcribe_voice(file_path: str, fmt: str='ogg') -> str:
    """Voice notes are not transcribed (no STT backend configured)."""
    return '(voice notes are not supported — send text instead)'
