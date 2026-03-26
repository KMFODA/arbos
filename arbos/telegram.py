from __future__ import annotations
from datetime import datetime
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import time
from typing import Any
import requests
from . import runtime as runtime_state
from .bootstrap import _create_new_project, _migration_preflight
from .state import load_chatlog, log_chat
from .runtime import _reload_runtime_config
from .env import _persist_env_var_with_comment, _process_pending_env, _redact_secrets, _save_to_encrypted_env
from .logs import _log
from .prompts import _path_for_display, format_available_env_vars_section, load_prompt
from .state import _agent_status_label, _claude_invocations_prompt_section, _save_agent, _write_go_flag
TELEGRAM_TEXT_MAX = 4096
TELEGRAM_SAFE_TEXT = 3900
TELEGRAM_INLINE_RETRY_MAX_SECONDS = 8
TELEGRAM_EDIT_MIN_INTERVAL_SECONDS = 6.0
TELEGRAM_HELP_TEXT = 'Arbos:\n- /loop <goal>\n- /pause \n- /resume\n- /force\n- /clear\n- /delay <mins>\n- /model <model>\n- /new <token>\n- /migrate <token>\n- /restart\n- /env <k> <v> <desc>\n'
_telegram_backoff_lock = threading.Lock()
_telegram_backoff_until: dict[str, float] = {}
_telegram_backoff_reason: dict[str, str] = {}
_telegram_backoff_last_skip_log: dict[str, float] = {}
_telegram_edit_state_lock = threading.Lock()
_telegram_edit_state: dict[tuple[int, int], dict[str, Any]] = {}

def _is_leading_slash_command(message) -> bool:
    """True if the message is a Telegram-style /command (entity or leading /)."""
    text = (message.text or '').strip()
    if not text.startswith('/'):
        return False
    entities = getattr(message, 'entities', None) or []
    for ent in entities:
        if getattr(ent, 'type', None) == 'bot_command' and getattr(ent, 'offset', None) == 0:
            return True
    return True

def _operator_message_thread_id() -> int | None:
    if not runtime_state.OPERATOR_THREAD_ID_FILE.exists():
        return None
    raw = runtime_state.OPERATOR_THREAD_ID_FILE.read_text().strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None

def _save_operator_telegram(message: Any) -> None:
    """Persist chat id and forum topic so loop steps and HTTP sends match the operator thread."""
    runtime_state.CHAT_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    runtime_state.CHAT_ID_FILE.write_text(str(message.chat.id))
    tid = getattr(message, 'message_thread_id', None)
    if tid is not None:
        runtime_state.OPERATOR_THREAD_ID_FILE.write_text(str(int(tid)))
    else:
        runtime_state.OPERATOR_THREAD_ID_FILE.unlink(missing_ok=True)

def _truncate_telegram_text(text: str, limit: int=TELEGRAM_SAFE_TEXT) -> str:
    """Trim for Telegram message body; append notice if truncated."""
    text = text or ''
    if len(text) <= limit:
        return text
    notice = f'\n\n… [truncated, {len(text)} chars total]'
    return text[:max(0, limit - len(notice))] + notice

def _tail_text_for_telegram(text: str, limit: int) -> str:
    """Keep the most recent text within *limit*, dropping older rollout lines first."""
    text = (text or '').strip()
    if limit <= 0:
        return ''
    if len(text) <= limit:
        return text
    ellipsis = '…\n'
    body_limit = max(0, limit - len(ellipsis))
    lines = text.splitlines()
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        add = len(line) + (1 if kept else 0)
        if kept and total + add > body_limit:
            break
        if not kept and len(line) > body_limit:
            kept = [line[-body_limit:]] if body_limit > 0 else []
            total = len(kept[0]) if kept else 0
            break
        kept.append(line)
        total += add
    kept.reverse()
    return ellipsis + '\n'.join(kept)

def _telegram_extract_error_info(source: Any) -> tuple[int | None, int | None, str]:
    """Best-effort parse of Telegram API failures from bot exceptions or raw JSON."""
    code: int | None = None
    retry_after: int | None = None
    description = ''

    def _coerce_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _consume_dict(data: dict[str, Any]) -> None:
        nonlocal code, retry_after, description
        if code is None:
            code = _coerce_int(data.get('error_code'))
        if not description and data.get('description') is not None:
            description = str(data.get('description'))
        params = data.get('parameters')
        if isinstance(params, dict) and retry_after is None:
            retry_after = _coerce_int(params.get('retry_after'))

    if isinstance(source, dict):
        _consume_dict(source)
    else:
        code = _coerce_int(getattr(source, 'error_code', None))
        desc_attr = getattr(source, 'description', None)
        if desc_attr:
            description = str(desc_attr)
        result_json = getattr(source, 'result_json', None)
        if isinstance(result_json, dict):
            _consume_dict(result_json)
        response = getattr(source, 'response', None)
        if response is not None:
            status_code = getattr(response, 'status_code', None)
            if code is None:
                code = _coerce_int(status_code)
            try:
                data = response.json()
            except Exception:
                data = None
            if isinstance(data, dict):
                _consume_dict(data)
            elif not description:
                text = getattr(response, 'text', '')
                if text:
                    description = str(text)

    text = description or str(source)
    if code is None:
        m = re.search(r'Error code:\s*(\d+)', text, flags=re.IGNORECASE)
        if m:
            code = int(m.group(1))
    if retry_after is None:
        m = re.search(r'retry(?:[_ ]after)?[^0-9]{0,16}(\d+)', text, flags=re.IGNORECASE)
        if m:
            retry_after = int(m.group(1))
    return (code, retry_after, (text or '').strip())

def _telegram_should_ignore_edit_error(source: Any) -> bool:
    (_, _, desc) = _telegram_extract_error_info(source)
    return 'message is not modified' in desc.lower()

def _telegram_backoff_bucket(context: str) -> str:
    raw = (context or '').strip()
    folded = re.sub(r'[^a-z0-9]+', '', raw.lower())
    if folded in {'editmessagetext', 'editmessagetextapierror', 'editmessagetextrequest'}:
        return 'edit_message_text'
    if folded in {'sendmessage', 'sendmessagefallback', 'sendmessagerequest'}:
        return 'send_message'
    return raw or 'telegram'

def _telegram_edit_gate(chat_id: int, message_id: int, body: str, *, force: bool=False) -> str:
    key = (int(chat_id), int(message_id))
    now = time.monotonic()
    with _telegram_edit_state_lock:
        state = _telegram_edit_state.setdefault(key, {'last_attempt_at': 0.0, 'last_body': ''})
        if body == state.get('last_body', ''):
            return 'duplicate'
        if (not force) and now - float(state.get('last_attempt_at', 0.0) or 0.0) < TELEGRAM_EDIT_MIN_INTERVAL_SECONDS:
            return 'throttled'
        state['last_attempt_at'] = now
    return 'send'

def _telegram_record_edit_success(chat_id: int, message_id: int, body: str) -> None:
    key = (int(chat_id), int(message_id))
    with _telegram_edit_state_lock:
        state = _telegram_edit_state.setdefault(key, {'last_attempt_at': 0.0, 'last_body': ''})
        state['last_body'] = body
        state['last_attempt_at'] = time.monotonic()

def _telegram_note_backoff(context: str, code: int | None, retry_after: int | None, desc: str) -> None:
    if not retry_after or retry_after <= 0:
        return
    now = time.monotonic()
    until = now + retry_after
    bucket = _telegram_backoff_bucket(context)
    with _telegram_backoff_lock:
        current_until = _telegram_backoff_until.get(bucket, 0.0)
        if until > current_until:
            _telegram_backoff_until[bucket] = until
            _telegram_backoff_reason[bucket] = f'{context} rate limited ({code or "?"}: {desc[:120]})'
    _log(f'telegram {context} rate limited; suppressing outbound requests for {retry_after}s ({desc[:220]})')

def _telegram_skip_due_to_backoff(context: str) -> bool:
    now = time.monotonic()
    bucket = _telegram_backoff_bucket(context)
    with _telegram_backoff_lock:
        remaining = _telegram_backoff_until.get(bucket, 0.0) - now
        reason = _telegram_backoff_reason.get(bucket, '')
        last_skip_log = _telegram_backoff_last_skip_log.get(bucket, 0.0)
        should_log = remaining > 0 and (now - last_skip_log >= 30.0)
        if should_log:
            _telegram_backoff_last_skip_log[bucket] = now
    if remaining <= 0:
        return False
    if should_log:
        _log(f'telegram {context} skipped during backoff; {int(remaining)}s remaining ({reason[:180]})')
    return True

def _telegram_should_retry(context: str, source: Any, attempt: int) -> bool:
    (code, retry_after, desc) = _telegram_extract_error_info(source)
    if code == 429:
        delay = max(1, retry_after or 1)
        if attempt == 1 and delay <= TELEGRAM_INLINE_RETRY_MAX_SECONDS:
            _log(f'telegram {context} hit 429; sleeping {delay}s before retry')
            time.sleep(delay)
            return True
        _telegram_note_backoff(context, code, delay, desc or 'Too Many Requests')
        return False
    if code in {500, 502, 503, 504} and attempt == 1:
        delay = 2
        _log(f'telegram {context} transient {code}; sleeping {delay}s before retry')
        time.sleep(delay)
        return True
    return False

def _telegram_bot_send_message(bot: Any, chat_id: int, text: str, **kwargs) -> Any | None:
    body = _truncate_telegram_text(_redact_secrets(text))[:TELEGRAM_TEXT_MAX]
    if _telegram_skip_due_to_backoff('send_message'):
        return None
    for attempt in (1, 2):
        try:
            return bot.send_message(chat_id, body, **kwargs)
        except Exception as exc:
            if _telegram_should_retry('send_message', exc, attempt):
                continue
            raise
    return None

def _telegram_bot_edit_message_text(bot: Any, text: str, chat_id: int, message_id: int, *, force: bool=False) -> bool:
    body = _truncate_telegram_text(_redact_secrets(text))[:TELEGRAM_TEXT_MAX]
    edit_gate = _telegram_edit_gate(chat_id, message_id, body, force=force)
    if edit_gate == 'duplicate':
        return True
    if edit_gate != 'send':
        return False
    if _telegram_skip_due_to_backoff('edit_message_text'):
        return False
    for attempt in (1, 2):
        try:
            bot.edit_message_text(body, chat_id, message_id)
            _telegram_record_edit_success(chat_id, message_id, body)
            return True
        except Exception as exc:
            if _telegram_should_ignore_edit_error(exc):
                _telegram_record_edit_success(chat_id, message_id, body)
                return True
            if _telegram_should_retry('edit_message_text', exc, attempt):
                continue
            raise
    return False

def _telegram_api_request(method: str, token: str, payload: dict[str, Any], *, timeout: int=15, force: bool=False) -> dict[str, Any] | None:
    bucket = _telegram_backoff_bucket(method)
    if bucket == 'edit_message_text':
        try:
            chat_id = int(payload.get('chat_id'))
            message_id = int(payload.get('message_id'))
        except (TypeError, ValueError):
            chat_id = None
            message_id = None
        body = str(payload.get('text') or '')[:TELEGRAM_TEXT_MAX]
        if chat_id is not None and message_id is not None:
            edit_gate = _telegram_edit_gate(chat_id, message_id, body, force=force)
            if edit_gate != 'send':
                desc = 'message is not modified' if edit_gate == 'duplicate' else 'local edit throttle'
                return {'ok': False, 'description': desc, '__arbos_local_skip': edit_gate}
    if _telegram_skip_due_to_backoff(method):
        return None
    url = f'https://api.telegram.org/bot{token}/{method}'
    for attempt in (1, 2):
        try:
            response = requests.post(url, json=payload, timeout=timeout)
        except Exception as exc:
            if _telegram_should_retry(method, exc, attempt):
                continue
            _log(f'telegram {method} request failed: {str(exc)[:200]}')
            return None
        try:
            data = response.json()
        except Exception:
            data = None
        if response.ok:
            if isinstance(data, dict):
                (ok, _) = _telegram_result_ok(data)
                if not ok:
                    if _telegram_should_retry(method, data, attempt):
                        continue
                    (_, _, desc) = _telegram_extract_error_info(data)
                    _log(f'telegram {method} API error: {desc[:300]}')
                    return None
                return data
            _log(f'telegram {method} returned non-JSON success response')
            return None
        err_source: Any = data if isinstance(data, dict) else response
        if bucket == 'edit_message_text' and _telegram_should_ignore_edit_error(err_source):
            return {'ok': False, 'description': 'message is not modified'}
        if _telegram_should_retry(method, err_source, attempt):
            continue
        (_, _, desc) = _telegram_extract_error_info(err_source)
        _log(f'telegram {method} API error: {(desc or response.text)[:300]}')
        return None
    return None

def _telegram_send_message_fallback(bot: Any, chat_id: int, text: str, base_kw: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Send a Telegram message; drop reply/thread kwargs if the API rejects the first attempt."""
    body = _truncate_telegram_text(_redact_secrets(text))[:TELEGRAM_TEXT_MAX]
    attempts: list[dict[str, Any]] = []
    attempts.append(dict(base_kw))
    no_reply = {k: v for (k, v) in base_kw.items() if k != 'reply_to_message_id'}
    if no_reply != attempts[-1]:
        attempts.append(no_reply)
    tid = base_kw.get('message_thread_id')
    if tid is not None:
        thread_only: dict[str, Any] = {'message_thread_id': tid}
        if thread_only != attempts[-1]:
            attempts.append(thread_only)
    if attempts[-1]:
        attempts.append({})
    last_exc: Exception | None = None
    for kw in attempts:
        try:
            msg = _telegram_bot_send_message(bot, chat_id, body, **kw)
            if msg is not None:
                return (msg, kw)
        except Exception as e:
            last_exc = e
    if last_exc is None and _telegram_skip_due_to_backoff('send_message'):
        raise RuntimeError('Telegram send_message suppressed during API backoff')
    assert last_exc is not None
    raise last_exc

def _telegram_result_ok(data: Any) -> tuple[bool, str]:
    """Telegram often returns HTTP 200 with {"ok": false}."""
    if not isinstance(data, dict):
        return (False, str(data)[:300])
    if data.get('ok'):
        return (True, '')
    desc = str(data.get('description', data))
    return (False, desc)

def _streaming_empty_summary(returncode: int, stderr_output: str, attempts: int) -> str:
    """Telegram body when Claude returns no assistant text."""
    lines = [f'No assistant text after {attempts} attempt(s).', f'exit_code={returncode}']
    if stderr_output.strip():
        lines.append('--- stderr ---')
        tail = stderr_output.strip()
        lines.append(tail[-3200:] if len(tail) > 3200 else tail)
    else:
        lines.append('stderr=(empty) — check Arbos logs for details.')
    return '\n'.join(lines)

def _build_agent_failure_detail(result: subprocess.CompletedProcess) -> str:
    """Human-readable diagnostics when a Claude run did not succeed."""
    lines: list[str] = [f'exit_code={result.returncode}']
    err = (result.stderr or '').strip()
    if err.startswith('(timed out') or 'timed out after' in err[:120]:
        lines.append('cause=idle_watchdog (no stdout/stderr for CLAUDE_TIMEOUT; set CLAUDE_TIMEOUT higher or 0 to disable)')
    if err:
        lines.append('--- stderr ---')
        lines.append(err[-3500:] if len(err) > 3500 else err)
    else:
        lines.append('stderr=(empty)')
    out = (result.stdout or '').strip()
    if out:
        lines.append('--- stdout excerpt ---')
        lines.append(out[:2000] + ('…' if len(out) > 2000 else ''))
    return '\n'.join(lines)

def _step_update_target() -> tuple[str, str, int | None] | None:
    token = os.getenv('TAU_BOT_TOKEN')
    if not token:
        _log('step update skipped: TAU_BOT_TOKEN not set')
        return None
    if not runtime_state.CHAT_ID_FILE.exists():
        _log('step update skipped: chat_id.txt not found')
        return None
    chat_id = runtime_state.CHAT_ID_FILE.read_text().strip()
    if not chat_id:
        _log('step update skipped: empty chat_id.txt')
        return None
    return (token, chat_id, _operator_message_thread_id())

def _send_telegram_text(text: str, *, target: tuple[str, str, int | None] | None=None) -> bool:
    target = target or _step_update_target()
    if not target:
        return False
    (token, chat_id, thread_id) = target
    text = _redact_secrets(text)
    payload: dict[str, Any] = {'chat_id': chat_id, 'text': text[:TELEGRAM_TEXT_MAX]}
    if thread_id is not None:
        payload['message_thread_id'] = thread_id
    data = _telegram_api_request('sendMessage', token, payload, timeout=15)
    if not isinstance(data, dict):
        return False
    (ok, desc) = _telegram_result_ok(data)
    if not ok:
        _log(f'telegram sendMessage API error: {desc[:300]}')
        return False
    log_chat('bot', text[:1000])
    _log('telegram message sent')
    return True

def _send_telegram_new(text: str, *, target: tuple[str, str, int | None] | None=None) -> int | None:
    """Send a new Telegram message and return its message_id."""
    target = target or _step_update_target()
    if not target:
        return None
    (token, chat_id, thread_id) = target
    text = _redact_secrets(text)
    payload: dict[str, Any] = {'chat_id': chat_id, 'text': text[:TELEGRAM_TEXT_MAX]}
    if thread_id is not None:
        payload['message_thread_id'] = thread_id
    data = _telegram_api_request('sendMessage', token, payload, timeout=15)
    if not isinstance(data, dict):
        return None
    (ok, desc) = _telegram_result_ok(data)
    if not ok:
        _log(f'telegram sendMessage API error: {desc[:300]}')
        return None
    return data.get('result', {}).get('message_id')

def _edit_telegram_text(message_id: int, text: str, *, target: tuple[str, str, int | None] | None=None, force: bool=False) -> bool:
    """Edit an existing Telegram message."""
    target = target or _step_update_target()
    if not target:
        return False
    (token, chat_id, thread_id) = target
    text = _redact_secrets(text)
    payload: dict[str, Any] = {'chat_id': chat_id, 'message_id': message_id, 'text': text[:TELEGRAM_TEXT_MAX]}
    if thread_id is not None:
        payload['message_thread_id'] = thread_id
    data = _telegram_api_request('editMessageText', token, payload, timeout=15, force=force)
    if not isinstance(data, dict):
        return False
    local_skip = data.get('__arbos_local_skip')
    if local_skip == 'duplicate':
        return True
    if local_skip:
        return False
    (ok, desc) = _telegram_result_ok(data)
    if ok or 'message is not modified' in desc.lower():
        _telegram_record_edit_success(int(chat_id), int(message_id), text[:TELEGRAM_TEXT_MAX])
        return True
    _log(f'telegram editMessageText API error: {desc[:300]}')
    return False

def _send_telegram_document(file_path: str, caption: str='', *, target: tuple[str, str, int | None] | None=None) -> bool:
    """Send a file as a Telegram document."""
    target = target or _step_update_target()
    if not target:
        return False
    (token, chat_id, thread_id) = target
    caption = _redact_secrets(caption)[:1024]
    data: dict[str, Any] = {'chat_id': chat_id, 'caption': caption}
    if thread_id is not None:
        data['message_thread_id'] = str(thread_id)
    try:
        with open(file_path, 'rb') as f:
            response = requests.post(f'https://api.telegram.org/bot{token}/sendDocument', data=data, files={'document': (Path(file_path).name, f)}, timeout=60)
        response.raise_for_status()
        _log(f'telegram document sent: {Path(file_path).name}')
        log_chat('bot', f'[sent file: {Path(file_path).name}] {caption}')
        return True
    except Exception as exc:
        _log(f'telegram document send failed: {str(exc)[:120]}')
        return False

def _send_telegram_photo(file_path: str, caption: str='', *, target: tuple[str, str, int | None] | None=None) -> bool:
    """Send an image as a Telegram photo (compressed)."""
    target = target or _step_update_target()
    if not target:
        return False
    (token, chat_id, thread_id) = target
    caption = _redact_secrets(caption)[:1024]
    data: dict[str, Any] = {'chat_id': chat_id, 'caption': caption}
    if thread_id is not None:
        data['message_thread_id'] = str(thread_id)
    try:
        with open(file_path, 'rb') as f:
            response = requests.post(f'https://api.telegram.org/bot{token}/sendPhoto', data=data, files={'photo': (Path(file_path).name, f)}, timeout=60)
        response.raise_for_status()
        _log(f'telegram photo sent: {Path(file_path).name}')
        log_chat('bot', f'[sent photo: {Path(file_path).name}] {caption}')
        return True
    except Exception as exc:
        _log(f'telegram photo send failed: {str(exc)[:120]}')
        return False

def _download_telegram_file(bot, file_id: str, filename: str) -> Path:
    """Download a file from Telegram and save it to FILES_DIR."""
    runtime_state.FILES_DIR.mkdir(parents=True, exist_ok=True)
    file_info = bot.get_file(file_id)
    downloaded = bot.download_file(file_info.file_path)
    save_path = runtime_state.FILES_DIR / filename
    if save_path.exists():
        (stem, suffix) = (save_path.stem, save_path.suffix)
        ts = datetime.now().strftime('%H%M%S')
        save_path = runtime_state.FILES_DIR / f'{stem}_{ts}{suffix}'
    save_path.write_bytes(downloaded)
    _log(f'saved telegram file: {save_path.name} ({len(downloaded)} bytes)')
    return save_path

def _recent_context(max_chars: int=6000) -> str:
    """Collect recent rollouts under the active project runs directory."""
    parts: list[str] = []
    total = 0
    all_runs: list[tuple[str, Path]] = []
    if runtime_state.RUNS_DIR.exists():
        for d in runtime_state.RUNS_DIR.iterdir():
            if d.is_dir():
                all_runs.append((d.name, d))
    all_runs.sort(key=lambda x: x[1].name, reverse=True)
    for (label, run_dir) in all_runs:
        f = run_dir / 'rollout.md'
        if f.exists():
            content = f.read_text()[:2000]
            hdr = f'\n--- rollout.md ({label}) ---\n'
            if total + len(hdr) + len(content) > max_chars:
                return ''.join(parts)
            parts.append(hdr + content)
            total += len(hdr) + len(content)
        if total > max_chars:
            break
    return ''.join(parts)

def _telegram_message_excerpt(msg: Any, max_len: int=2000) -> str:
    """Best-effort text from a Telegram Message-like object (telebot; duck-typed)."""
    if msg is None:
        return ''
    t = (getattr(msg, 'text', None) or '').strip()
    if t:
        return t[:max_len] + '…' if len(t) > max_len else t
    cap = (getattr(msg, 'caption', None) or '').strip()
    if cap:
        return cap[:max_len] + '…' if len(cap) > max_len else cap
    if getattr(msg, 'document', None):
        doc = msg.document
        fn = getattr(doc, 'file_name', None) or 'file'
        return f'[Document: {fn}]'
    if getattr(msg, 'photo', None):
        return '[Photo]'
    if getattr(msg, 'voice', None) or getattr(msg, 'audio', None):
        return '[Voice or audio message]'
    return '[Message without inline text]'

def _operator_telegram_reply_nudge(current_from_user_id: int | None, parent_msg: Any) -> str:
    """Section for the operator prompt when Telegram reply-to is used."""
    pu = parent_msg.from_user.id if getattr(parent_msg, 'from_user', None) else None
    if current_from_user_id is not None and pu == current_from_user_id:
        who = "the operator's own earlier message in this chat"
    else:
        who = 'a previous Arbos (assistant) message in this chat'
    excerpt = _telegram_message_excerpt(parent_msg)
    return f"## Telegram reply context\n\nThe operator used **Telegram's reply** to reference a specific bubble. They are responding to **{who}**, quoted below. The **Operator message** section at the end is their **new** text; treat it as a follow-up to the quoted message when that is the natural reading.\n\n**Quoted message:**\n{excerpt}"

def _telegram_reply_context_for_prompt(message: Any) -> str | None:
    parent = getattr(message, 'reply_to_message', None)
    if parent is None:
        return None
    uid = message.from_user.id if getattr(message, 'from_user', None) else None
    return _operator_telegram_reply_nudge(uid, parent)

def _build_operator_prompt(user_text: str, *, reply_context: str | None=None) -> str:
    """Build prompt for the CLI agent to handle any operator request."""
    chatlog = load_chatlog(max_chars=4000)
    context_root = _path_for_display(runtime_state.CONTEXT_DIR)
    workspace_root = _path_for_display(runtime_state.WORKSPACE_DIR)
    inbox_path = _path_for_display(runtime_state.INBOX_FILE)
    state_path = _path_for_display(runtime_state.STATE_FILE)
    env_pending_path = _path_for_display(runtime_state.ENV_PENDING_FILE)
    runs_path = _path_for_display(runtime_state.RUNS_DIR)
    invocations_path = _path_for_display(runtime_state.CLAUDE_INVOCATIONS_FILE)
    files_path = _path_for_display(runtime_state.FILES_DIR)
    restart_path = _path_for_display(runtime_state.RESTART_FLAG)
    parts = [f"""You are the operator interface for Arbos, a coding agent running in a loop via pm2.\nThe operator communicates with you through Telegram. Be concise and direct.\nWhen the operator asks you to do something, do it by modifying the relevant files.\nWhen the operator asks a question, answer from the available context.\n\n## Security\n\nNEVER read, output, or reveal the contents of `.env`, `.env.enc`, or any secret/key/token values.\nDo not include API keys, passwords, seed phrases, or credentials in any response.\nIf asked to show secrets, refuse. The .env file is encrypted; do not attempt to decrypt it.\n\n{format_available_env_vars_section()}\n\n## Single agent loop\n\nOne agent loop uses flat files under `{context_root}/`: GOAL.md, GO.md, STATE.md, INBOX.md, and `{runs_path}/<timestamp>/`.\n- **Workspace**: code edits should happen under `{workspace_root}/`.\n- **GOAL.md**: loop instructions (set by /loop).\n- **GO.md**: run flag — must exist for steps to execute. /loop and /resume create it; /pause deletes it.\nTelegram: /loop, /pause, /resume, /force, /clear, /delay (see /help).\n- **Message the agent**: append a timestamped line to `{inbox_path}`.\n- **Update agent state**: write to `{state_path}`.\n- **Set system prompt**: write to `PROMPT.md`.\n- **Set env variable**: write `KEY='VALUE'` lines (one per line) to `{env_pending_path}`. They are picked up automatically and persisted.\n- **View logs**: read files in `{runs_path}/<timestamp>/` (rollout.md, logs.txt).\n- **Inspect Claude invocations**: read `{invocations_path}` for active/recent subprocess metadata, and `{runs_path}/<timestamp>/invocation-<attempt>.json` for per-run details.\n- **Kill a stuck Claude run**: use the `pid` from the invocation metadata and terminate that specific subprocess.\n- **Modify code & restart**: edit code files, then run `touch {restart_path}`.\n- **Send follow-up**: run `arbos -p {runtime_state.PROJECT_NAME} send "your text here"`.\n- **Send file to operator**: run `arbos -p {runtime_state.PROJECT_NAME} sendfile path/to/file [--caption 'text'] [--photo]`.\n- **Received files**: operator-sent files are saved in `{files_path}/` and their path is shown in the message."""]
    with runtime_state._agent_lock:
        gs = runtime_state._agent
        status = _agent_status_label(gs)
        delay_note = f'{gs.delay_minutes}m between steps' if gs.delay_minutes else 'no delay between steps'
        goal_text = runtime_state.GOAL_FILE.read_text().strip()[:200] if runtime_state.GOAL_FILE.exists() else '(empty)'
        go_line = 'yes (loop may run steps)' if runtime_state.GO_FLAG_FILE.exists() else 'no (paused — create GO.md or /resume)'
        state_text = runtime_state.STATE_FILE.read_text().strip()[:200] if runtime_state.STATE_FILE.exists() else '(empty)'
        parts.append(f'## Agent [{status}] ({delay_note}, step {gs.step_count})\nCurrent goal (GOAL.md): {goal_text}\nRun flag (GO.md): {go_line}\nState (STATE.md): {state_text}')
    if chatlog:
        parts.append(chatlog)
    context = _recent_context(max_chars=4000)
    if context:
        parts.append(f'## Recent activity\n{context}')
    parts.append(_claude_invocations_prompt_section(limit=6))
    if reply_context:
        parts.append(reply_context)
    parts.append(f'## Operator message\n{user_text}')
    return '\n\n'.join(parts)

def _is_owner(user_id: int) -> bool:
    owner = os.environ.get('TELEGRAM_OWNER_ID', '').strip()
    if not owner:
        return False
    return str(user_id) == owner

def _enroll_owner(user_id: int):
    """Auto-enroll the first /start user as the owner and persist."""
    owner_id = str(user_id)
    os.environ['TELEGRAM_OWNER_ID'] = owner_id
    env_path = runtime_state.ENV_FILE
    if env_path.exists():
        existing = env_path.read_text()
        if 'TELEGRAM_OWNER_ID' not in existing:
            with open(env_path, 'a') as f:
                f.write(f"\nTELEGRAM_OWNER_ID='{owner_id}'\n")
    elif runtime_state.ENV_ENC_FILE.exists():
        _save_to_encrypted_env('TELEGRAM_OWNER_ID', owner_id)
    _log(f'enrolled owner: {owner_id}')

def run_bot():
    from .claude import _summarize_goal, _write_claude_settings, run_agent_streaming, transcribe_voice
    from .loop import _clear_agent_runtime_history, _ensure_agent_thread, _kill_child_procs, _kill_registered_claude_procs, _pre_send_normal_step_bubble, run_step
    """Run the Telegram bot."""
    token = os.getenv('TAU_BOT_TOKEN')
    if not token:
        _log('TAU_BOT_TOKEN not set; add it to .env and restart')
        sys.exit(1)
    import telebot

    class _TelegramNetworkHandler(telebot.ExceptionHandler):
        """Treat DNS/network failures as handled so threaded polling backs off instead of raising."""

        def handle(self, exception):
            if isinstance(exception, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
                _log(f'Telegram API unreachable (network/DNS); backing off ({type(exception).__name__})')
                return True
            return False
    bot = telebot.TeleBot(token, exception_handler=_TelegramNetworkHandler())

    def _reply(message, text: str, **kwargs):
        """Send *text* as a Telegram reply to the user's message."""
        send_kw: dict[str, Any] = {'reply_to_message_id': message.message_id}
        tid = getattr(message, 'message_thread_id', None)
        if tid is not None:
            send_kw['message_thread_id'] = tid
        send_kw.update(kwargs)
        return bot.send_message(message.chat.id, text, **send_kw)

    def _reject(message):
        uid = message.from_user.id if message.from_user else None
        _log(f'rejected message from unauthorized user {uid}')
        if not os.environ.get('TELEGRAM_OWNER_ID', '').strip():
            _reply(message, 'Send /start to register as the owner.')
        else:
            _reply(message, 'Unauthorized.')

    @bot.message_handler(commands=['start'])
    def handle_start(message):
        uid = message.from_user.id if message.from_user else None
        if not os.environ.get('TELEGRAM_OWNER_ID', '').strip() and uid is not None:
            _enroll_owner(uid)
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        _reply(message, _truncate_telegram_text(TELEGRAM_HELP_TEXT.strip()))

    @bot.message_handler(commands=['help'])
    def handle_help(message):
        _reply(message, _truncate_telegram_text(TELEGRAM_HELP_TEXT.strip()))

    @bot.message_handler(commands=['status'])
    def handle_status(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        hp = _operator_health_payload()
        op = hp['operator']
        head = [f"Arbos {hp['status']} | uptime {hp['uptime_seconds']}s", f"Now: {op['phase']} — {op['detail']}", f"Activity clock: {op['seconds_since_activity']}s since last update (refreshes while Claude runs, during backoff waits, etc.)"]
        if op.get('last_error'):
            head.append(f"Last error (global): {op['last_error'][:450]}")
        if hp.get('degraded_reason'):
            head.append(f"Degraded: {hp['degraded_reason']}")
        banner = '\n'.join(head) + '\n\n'
        with runtime_state._agent_lock:
            gs = runtime_state._agent
        status = _agent_status_label(gs)
        goal_text = runtime_state.GOAL_FILE.read_text().strip()[:500] if runtime_state.GOAL_FILE.exists() else '(empty)'
        state_text = runtime_state.STATE_FILE.read_text().strip()[:500] if runtime_state.STATE_FILE.exists() else '(empty)'
        if gs.last_step_ok is None:
            step_out = 'no step completed yet'
        elif gs.last_step_ok:
            step_out = 'last step OK'
        else:
            step_out = 'last step FAILED'
        lines = [banner.rstrip(), f'Agent [{status}] (delay: {gs.delay_minutes}m, step {gs.step_count})', step_out, f"Last run: {gs.last_run or 'never'}", f"Last finished: {gs.last_finished or 'never'}"]
        if gs.last_step_error:
            lines.append(f'Last step error:\n{gs.last_step_error[:900]}')
        lines.extend(['', f'Loop: {goal_text}', f"Run flag: {('GO.md present' if runtime_state.GO_FLAG_FILE.exists() else 'GO.md absent')}", '', f'State: {state_text}', '', f'Total steps: {runtime_state._step_count}', f'Claude registry: {runtime_state.CLAUDE_INVOCATIONS_FILE}'])
        invocation_items = hp.get('claude_invocations', {}).get('items', [])
        if invocation_items:
            lines.extend(['', 'Claude invocations:'])
            for item in invocation_items[:6]:
                age = item.get('uptime_seconds')
                if age is None:
                    age = item.get('duration_seconds')
                lines.append(f"- {item.get('step_label') or item.get('phase') or item.get('kind')} [{item.get('status')}] pid={item.get('pid')} age={age}s run_dir={item.get('run_dir') or '(none)'}")
        _reply(message, '\n'.join(lines))

    @bot.message_handler(commands=['pause'])
    def handle_pause(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        if not runtime_state.GO_FLAG_FILE.exists():
            _reply(message, 'Already paused (no GO.md).')
            return
        runtime_state.GO_FLAG_FILE.unlink(missing_ok=True)
        with runtime_state._agent_lock:
            gs = runtime_state._agent
            gs.paused = True
            _save_agent()
        gs.wake.set()
        _reply(message, f'Paused (removed {_path_for_display(runtime_state.GO_FLAG_FILE)}). GOAL.md unchanged. /resume to run again.')
        _log('agent paused via /pause (GO.md removed)')

    @bot.message_handler(commands=['resume'])
    def handle_resume(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        if not runtime_state.GOAL_FILE.exists() or not runtime_state.GOAL_FILE.read_text().strip():
            _reply(message, 'No goal in GOAL.md — use /loop <goal> first.')
            return
        if runtime_state.GO_FLAG_FILE.exists():
            _reply(message, f'Already resumed: {_path_for_display(runtime_state.GO_FLAG_FILE)} is already present, so the loop is enabled.')
            _log('agent /resume ignored (GO.md already present)')
            return
        _write_go_flag()
        with runtime_state._agent_lock:
            gs = runtime_state._agent
            gs.stop_event.clear()
            gs.paused = False
            gs.started = True
            _save_agent()
        gs.wake.set()
        _ensure_agent_thread()
        _reply(message, f'Resumed: created {_path_for_display(runtime_state.GO_FLAG_FILE)} and woke the agent loop.')
        _log('agent resumed via /resume (GO.md created)')

    @bot.message_handler(commands=['force'])
    def handle_force(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        if not runtime_state.GOAL_FILE.exists() or not runtime_state.GOAL_FILE.read_text().strip():
            _reply(message, 'No goal in GOAL.md — use /loop <goal> first.')
            return
        with runtime_state._agent_lock:
            gs = runtime_state._agent
            astep = gs.step_count if gs.step_count > 0 else 1
        prompt = load_prompt(consume_inbox=False, agent_step=astep)
        if not prompt.strip():
            _reply(message, 'Prompt is empty.')
            return
        _reply(message, 'Starting a forced step in the background — watch for a new **Step (forced)** bubble that streams the rollout.')

        def _run_force_step():
            try:
                run_step(prompt, 0, agent_step=astep, force_step=True)
            except Exception as exc:
                _log(f'/force step crashed: {type(exc).__name__}: {exc!s}')
        threading.Thread(target=_run_force_step, daemon=True, name='force-step').start()

    @bot.message_handler(commands=['delay'])
    def handle_delay(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        args = (message.text or '').split()
        if len(args) < 2:
            _reply(message, 'Usage: /delay <minutes>')
            return
        try:
            minutes = int(args[1])
        except ValueError:
            _reply(message, 'Usage: /delay <minutes> (integer)')
            return
        if minutes < 0:
            _reply(message, 'Delay must be >= 0.')
            return
        with runtime_state._agent_lock:
            runtime_state._agent.delay_minutes = minutes
            _save_agent()
        _reply(message, f'Delay set to {minutes} minute(s) between successful steps.')
        _log(f'delay set to {minutes}m via /delay')

    @bot.message_handler(commands=['model'])
    def handle_model(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        parts = (message.text or '').split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            override = os.environ.get('CLAUDE_MODEL', '').strip()
            source = 'project override' if override else 'built-in default'
            _reply(message, '\n'.join([f'Current model: `{runtime_state.CLAUDE_MODEL}`', f'Source: {source}', f'Built-in default: `{runtime_state.DEFAULT_CLAUDE_MODEL}`', 'Usage: /model <provider/model>']))
            return
        model = ' '.join(parts[1].split()).strip()
        if not model or any((ch.isspace() for ch in model)):
            _reply(message, 'Usage: /model <provider/model> (no spaces)')
            return
        (ok, msg) = _persist_env_var_with_comment('CLAUDE_MODEL', model, 'Default model for this bot/project.')
        if not ok:
            _reply(message, f'Error: {msg}')
            return
        _reload_runtime_config()
        _write_claude_settings()
        _reply(message, f'Default model for this project is now `{runtime_state.CLAUDE_MODEL}`. New steps will use it.')
        _log(f'/model set CLAUDE_MODEL={runtime_state.CLAUDE_MODEL!r}')

    @bot.message_handler(commands=['env'])
    def handle_env(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        try:
            parts = shlex.split(message.text or '')
        except ValueError as exc:
            _reply(message, f'Invalid /env syntax: {exc}')
            return
        if len(parts) < 3:
            _reply(message, 'Usage: /env KEY "VALUE" [DESCRIPTION]\nQuote VALUE when it contains spaces, such as mnemonic phrases.')
            return
        (_, key, value, *description_parts) = parts
        description = ' '.join(description_parts).strip() or 'Set via Telegram /env command.'
        (ok, msg) = _persist_env_var_with_comment(key, value, description)
        _reply(message, msg if ok else f'Error: {msg}')
        if ok:
            _log(f'/env persisted key={key!r}')
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception as exc:
                _log(f'/env: could not delete operator message (token may remain in chat): {exc!r}')

    @bot.message_handler(commands=['new'])
    def handle_new(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        parts = (message.text or '').split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            _reply(message, 'Usage: /new <bot token>')
            return
        new_token = parts[1].strip()
        try:
            result = _create_new_project(new_token)
        except Exception as exc:
            _reply(message, _truncate_telegram_text(f'New bot failed: {_redact_secrets(str(exc))[:800]}'))
            _log(f'/new failed: {type(exc).__name__}: {_redact_secrets(str(exc))[:200]}')
            return
        _reply(message, '\n'.join([f'Created @{result.identity.username}.', f'Chat: https://t.me/{result.identity.username}', f'CWD: {_path_for_display(result.project_dir)}', f"Workspace: {_path_for_display(result.project_dir / 'workspace')}", f'PM2: {result.pm2_name}', 'Fresh workspace created. Same owner copied. Open the new bot chat and send /start, then /loop ...']))
        _log(f'/new created @{result.identity.username} pm2={result.pm2_name}')

    @bot.message_handler(commands=['migrate'])
    def handle_migrate(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        _log('/migrate requested')
        parts = (message.text or '').split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            _reply(message, 'Usage: /migrate <bot token>')
            return
        new_token = parts[1].strip()
        try:
            preflight = _migration_preflight(runtime_state.PROJECT_DIR, new_bot_token=new_token)
            blocking = preflight.get('blocking_companion_pm2') or []
            if blocking:
                lines = ['Migration blocked: stop these companion pm2 services first.']
                lines.extend((f"- {item['name']} [{item['status']}]" for item in blocking[:12]))
                if len(blocking) > 12:
                    lines.append(f'- ... and {len(blocking) - 12} more')
                _reply(message, '\n'.join(lines))
                _log('/migrate blocked by companion pm2 services')
                return
            arbos_bin = runtime_state.CODE_DIR / '.venv' / 'bin' / 'arbos'
            env_file = runtime_state.PROJECT_DIR / '.env'
            activate_path = runtime_state.CODE_DIR / '.venv' / 'bin' / 'activate'
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            migrate_log = runtime_state.CONTEXT_LOGS_DIR / f'migrate-{ts}.log'
            command = ' '.join([
                'sleep 1;',
                'set -a;',
                f'[ -f {shlex.quote(str(env_file))} ] && source {shlex.quote(str(env_file))};',
                'set +a;',
                f'source {shlex.quote(str(activate_path))};',
                shlex.quote(str(arbos_bin)),
                '-p',
                shlex.quote(str(runtime_state.PROJECT_DIR)),
                'migrate-bot-token',
                '--bot-token',
                shlex.quote(new_token),
                '>',
                shlex.quote(str(migrate_log)),
                '2>&1',
            ])
            subprocess.Popen(
                ['bash', '-lc', command],
                cwd=runtime_state.CODE_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            _reply(message, _truncate_telegram_text(f'Migrate failed to start: {_redact_secrets(str(exc))[:800]}'))
            _log(f'/migrate failed to start: {type(exc).__name__}: {_redact_secrets(str(exc))[:200]}')
            return
        _reply(message, '\n'.join([
            'Starting migration in the background.',
            f'Log: {_path_for_display(migrate_log)}',
            'This bot may stop replying once pm2 switches over.',
        ]))
        _log(f'/migrate launched detached migration subprocess log={migrate_log}')

    @bot.message_handler(commands=['loop'])
    def handle_loop(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        text = (message.text or '').split(None, 1)
        if len(text) < 2 or not text[1].strip():
            _reply(message, 'Usage: /loop GOAL')
            return
        goal_text = text[1].strip()
        msg = _reply(message, 'Starting loop...')
        runtime_state.CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
        runtime_state.RUNS_DIR.mkdir(parents=True, exist_ok=True)
        runtime_state.GOAL_FILE.write_text(goal_text)
        _write_go_flag()
        if not runtime_state.STATE_FILE.exists():
            runtime_state.STATE_FILE.write_text('')
        if not runtime_state.INBOX_FILE.exists():
            runtime_state.INBOX_FILE.write_text('')
        with runtime_state._agent_lock:
            next_global = runtime_state._step_count + 1
        loop_pre_id = _pre_send_normal_step_bubble(1, next_global)
        with runtime_state._pending_step_msg_lock:
            runtime_state._pending_step_msg_id = loop_pre_id
        with runtime_state._agent_lock:
            gs = runtime_state._agent
            gs.stop_event.clear()
            gs.summary = goal_text[:80] + ('…' if len(goal_text) > 80 else '') or '…'
            gs.goal_hash = ''
            gs.started = True
            gs.paused = False
            _save_agent()
        gs.wake.set()
        _ensure_agent_thread()
        summary = _summarize_goal(goal_text)
        with runtime_state._agent_lock:
            gs = runtime_state._agent
            gs.summary = summary
            _save_agent()
        edit_kw: dict[str, Any] = {}
        _tid = getattr(message, 'message_thread_id', None)
        if _tid is not None:
            edit_kw['message_thread_id'] = _tid
        bot.edit_message_text(f'Loop set: {summary}\nRunning (GO.md created; /pause removes it).', message.chat.id, msg.message_id, **edit_kw)
        _log(f'loop set ({len(goal_text)} chars), auto-start: {summary}')

    @bot.message_handler(commands=['clear'])
    def handle_clear(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        with runtime_state._agent_lock:
            gs = runtime_state._agent
            gs.stop_event.set()
            gs.wake.set()
            thread = gs.thread
            gs.started = False
            gs.paused = False
            gs.summary = ''
            gs.delay_minutes = 0
            gs.step_count = 0
            gs.goal_hash = ''
            gs.last_run = ''
            gs.last_finished = ''
            gs.last_step_ok = None
            gs.last_step_error = ''
        _kill_child_procs()
        killed = _kill_registered_claude_procs(detail='killed via /clear')
        if thread and thread.is_alive():
            thread.join(timeout=8)
        with runtime_state._agent_lock:
            gs.stop_event.clear()
            gs.thread = None
        _clear_agent_runtime_history()
        with runtime_state._pending_step_msg_lock:
            runtime_state._pending_step_msg_id = None
        _reply(message, f'Loop cleared (goal, invocations, runs, logs, and chat history reset). Killed {killed} live Claude process(es). Use /loop to start again.')
        _log('runtime cleared via /clear')

    @bot.message_handler(commands=['restart'])
    def handle_restart(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        _reply(message, 'Restarting ...')
        _log('restart requested via /restart command')
        _kill_child_procs()
        runtime_state.RESTART_FLAG.touch()

    @bot.message_handler(commands=['update'])
    def handle_update(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        msg = _reply(message, 'Pulling latest changes...')
        try:
            r = subprocess.run(['git', 'pull', '--ff-only'], cwd=runtime_state.CODE_DIR, capture_output=True, text=True, timeout=30)
            output = (r.stdout.strip() + '\n' + r.stderr.strip()).strip()
            if r.returncode != 0:
                bot.edit_message_text(f'Git pull failed:\n{output[:3800]}', message.chat.id, msg.message_id)
                _log(f'update failed: {output[:200]}')
                return
            bot.edit_message_text(f'Pulled:\n{output[:3800]}\n\nRestarting...', message.chat.id, msg.message_id)
            _log(f'update pulled: {output[:200]}')
        except Exception as exc:
            bot.edit_message_text(f'Git pull error: {str(exc)[:3800]}', message.chat.id, msg.message_id)
            _log(f'update error: {str(exc)[:200]}')
            return
        _kill_child_procs()
        runtime_state.RESTART_FLAG.touch()

    @bot.message_handler(content_types=['voice', 'audio'])
    def handle_voice(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        _reply(message, 'Transcribing voice note...')
        voice_or_audio = message.voice or message.audio
        file_info = bot.get_file(voice_or_audio.file_id)
        downloaded = bot.download_file(file_info.file_path)
        ext = file_info.file_path.rsplit('.', 1)[-1] if '.' in file_info.file_path else 'ogg'
        tmp_path = runtime_state.PROJECT_DIR / f'_voice_tmp.{ext}'
        tmp_path.write_bytes(downloaded)
        try:
            transcript = transcribe_voice(str(tmp_path), fmt=ext)
        finally:
            tmp_path.unlink(missing_ok=True)
        caption = message.caption or ''
        user_text = f'[Voice note transcription]: {transcript}'
        if caption:
            user_text += f'\n[Caption]: {caption}'
        log_chat('user', user_text[:1000])
        prompt = _build_operator_prompt(user_text, reply_context=_telegram_reply_context_for_prompt(message))

        def _run():
            response = run_agent_streaming(bot, prompt, message.chat.id, reply_to_message_id=message.message_id, message_thread_id=getattr(message, 'message_thread_id', None))
            log_chat('bot', response[:1000])
            _process_pending_env()
        threading.Thread(target=_run, daemon=True).start()

    @bot.message_handler(content_types=['document'])
    def handle_document(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        doc = message.document
        filename = doc.file_name or f'file_{doc.file_id[:8]}'
        saved_path = _download_telegram_file(bot, doc.file_id, filename)
        caption = message.caption or ''
        size_kb = doc.file_size / 1024 if doc.file_size else saved_path.stat().st_size / 1024
        user_text = f'[Sent file: {saved_path.name}] saved to {saved_path} ({size_kb:.1f} KB)'
        if caption:
            user_text += f'\n[Caption]: {caption}'
        is_text = False
        try:
            content = saved_path.read_text(errors='strict')
            if len(content) <= 8000:
                user_text += f'\n[File contents]:\n{content}'
                is_text = True
        except (UnicodeDecodeError, ValueError):
            pass
        if not is_text:
            user_text += '\n(Binary file — not included inline. Read it from the saved path if needed.)'
        log_chat('user', user_text[:1000])
        prompt = _build_operator_prompt(user_text, reply_context=_telegram_reply_context_for_prompt(message))

        def _run():
            response = run_agent_streaming(bot, prompt, message.chat.id, reply_to_message_id=message.message_id, message_thread_id=getattr(message, 'message_thread_id', None))
            log_chat('bot', response[:1000])
            _process_pending_env()
        threading.Thread(target=_run, daemon=True).start()

    @bot.message_handler(content_types=['photo'])
    def handle_photo(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        _save_operator_telegram(message)
        photo = message.photo[-1]
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'photo_{ts}.jpg'
        saved_path = _download_telegram_file(bot, photo.file_id, filename)
        caption = message.caption or ''
        user_text = f'[Sent photo: {saved_path.name}] saved to {saved_path}'
        if caption:
            user_text += f'\n[Caption]: {caption}'
        log_chat('user', user_text[:1000])
        prompt = _build_operator_prompt(user_text, reply_context=_telegram_reply_context_for_prompt(message))

        def _run():
            response = run_agent_streaming(bot, prompt, message.chat.id, reply_to_message_id=message.message_id, message_thread_id=getattr(message, 'message_thread_id', None))
            log_chat('bot', response[:1000])
            _process_pending_env()
        threading.Thread(target=_run, daemon=True).start()

    @bot.message_handler(func=lambda m: True)
    def handle_message(message):
        uid = message.from_user.id if message.from_user else None
        if not _is_owner(uid):
            _reject(message)
            return
        if _is_leading_slash_command(message):
            _reply(message, _truncate_telegram_text(TELEGRAM_HELP_TEXT.strip()))
            return
        _save_operator_telegram(message)
        raw_text = (message.text or '').strip()
        if not raw_text:
            _reply(message, 'Send a non-empty text message.')
            return
        log_chat('user', raw_text)
        prompt = _build_operator_prompt(raw_text, reply_context=_telegram_reply_context_for_prompt(message))

        def _run():
            response = run_agent_streaming(bot, prompt, message.chat.id, reply_to_message_id=message.message_id, message_thread_id=getattr(message, 'message_thread_id', None))
            log_chat('bot', response[:1000])
            _process_pending_env()
        threading.Thread(target=_run, daemon=True).start()
    _log('telegram bot started')
    while True:
        try:
            bot.infinity_polling(logger_level=None)
        except Exception as e:
            _log(f'bot polling error: {str(e)[:80]}, reconnecting in 5s')
            time.sleep(5)
