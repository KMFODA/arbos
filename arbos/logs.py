from __future__ import annotations
import json
from datetime import datetime
from typing import Any
from . import runtime as runtime_state
from .env import _redact_secrets

_TOOL_LABELS = {
    'Bash': 'Running command',
    'Read': 'Reading file',
    'Write': 'Writing file',
    'Edit': 'Editing file',
    'Glob': 'Searching files',
    'Grep': 'Searching code',
    'WebFetch': 'Fetching URL',
    'WebSearch': 'Web search',
    'Task': 'Running task',
}

def _file_log(msg: str):
    fh = getattr(runtime_state._tls, 'log_fh', None)
    if fh:
        with runtime_state._log_lock:
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            fh.write(f'{ts}  {_redact_secrets(msg)}\n')
            fh.flush()

def _log(msg: str, *, blank: bool=False):
    safe = _redact_secrets(msg)
    if blank:
        print(flush=True)
    print(safe, flush=True)
    _file_log(safe)

def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f'{seconds:.1f}s'
    (m, s) = divmod(int(seconds), 60)
    return f'{m}m {s}s'

def _approx_prompt_context_tokens(prompt: str) -> int:
    """Rough token count from UTF-8 length (~4 bytes/token); matches common heuristics."""
    if not prompt:
        return 0
    n = len(prompt.encode('utf-8'))
    return max(1, (n + 3) // 4)

def _reset_prompt_context_usage(prompt: str) -> None:
    with runtime_state._context_lock:
        runtime_state._context_est_tokens = _approx_prompt_context_tokens(prompt)
        runtime_state._context_paid_usage = runtime_state.ClaudeUsage()
        runtime_state._context_attempt_inflight = True

def _mark_prompt_context_attempt_started(prompt_est_tokens: int | None=None) -> None:
    with runtime_state._context_lock:
        if prompt_est_tokens is not None and prompt_est_tokens > 0:
            runtime_state._context_est_tokens = prompt_est_tokens
        runtime_state._context_attempt_inflight = True

def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0

def _parse_result_usage(raw_lines: list[str]) -> runtime_state.ClaudeUsage | None:
    for line in reversed(raw_lines):
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get('type') != 'result':
            continue
        usage = evt.get('usage')
        if not isinstance(usage, dict):
            continue
        return runtime_state.ClaudeUsage(input_tokens=_safe_int(usage.get('input_tokens')), output_tokens=_safe_int(usage.get('output_tokens')), cache_creation_input_tokens=_safe_int(usage.get('cache_creation_input_tokens')), cache_read_input_tokens=_safe_int(usage.get('cache_read_input_tokens')))
    return None

def _record_prompt_context_usage(usage: runtime_state.ClaudeUsage | None) -> runtime_state.ClaudeUsage:
    with runtime_state._context_lock:
        paid = runtime_state._context_paid_usage or runtime_state.ClaudeUsage()
        if usage is not None:
            paid = paid.plus(usage)
            runtime_state._context_paid_usage = paid
        runtime_state._context_attempt_inflight = False
        return paid

def _fmt_token_count(count: int) -> str:
    if count < 1000:
        return str(count)
    return f'{count / 1000:.1f}k'

def _fmt_context_for_header(est: int, paid_usage: runtime_state.ClaudeUsage | None, attempt_inflight: bool) -> str:
    paid_usage = paid_usage or runtime_state.ClaudeUsage()
    current_tokens = paid_usage.total_input_tokens + paid_usage.output_tokens
    if attempt_inflight and est > 0:
        current_tokens = max(current_tokens, est)
    elif current_tokens <= 0 and est > 0:
        current_tokens = est
    return _fmt_token_count(current_tokens)

def _arbos_response_header(elapsed_s: float) -> str:
    """First line on Telegram: elapsed + compact current token count."""
    with runtime_state._context_lock:
        est = runtime_state._context_est_tokens
        paid = runtime_state._context_paid_usage
        attempt_inflight = runtime_state._context_attempt_inflight
    return f'Arbos ( {fmt_duration(elapsed_s)}, {_fmt_context_for_header(est, paid, attempt_inflight)} )'

def _step_response_header(elapsed_s: float, step_label: str='Step') -> str:
    """First line on step bubbles: step label + elapsed + compact current token count."""
    with runtime_state._context_lock:
        est = runtime_state._context_est_tokens
        paid = runtime_state._context_paid_usage
        attempt_inflight = runtime_state._context_attempt_inflight
    return f'{step_label} ( {fmt_duration(elapsed_s)}, {_fmt_context_for_header(est, paid, attempt_inflight)} )'

def _format_tool_activity(tool_name: str, tool_input: dict) -> str:
    labels = globals().get('_TOOL_LABELS')
    if not isinstance(labels, dict):
        labels = {
            'Bash': 'Running command',
            'Read': 'Reading file',
            'Write': 'Writing file',
            'Edit': 'Editing file',
            'Glob': 'Searching files',
            'Grep': 'Searching code',
            'WebFetch': 'Fetching URL',
            'WebSearch': 'Web search',
            'Task': 'Running task',
        }
    label = labels.get(tool_name, tool_name)
    detail = ''
    if tool_name == 'Bash':
        detail = (tool_input.get('command') or '')[:80]
    elif tool_name in ('Read', 'Write', 'Edit'):
        detail = tool_input.get('file_path') or tool_input.get('path') or ''
        if detail:
            detail = detail.rsplit('/', 1)[-1]
    elif tool_name == 'Glob':
        detail = (tool_input.get('pattern') or tool_input.get('glob') or '')[:60]
    elif tool_name == 'Grep':
        detail = (tool_input.get('pattern') or tool_input.get('regex') or '')[:60]
    elif tool_name == 'WebFetch':
        detail = (tool_input.get('url') or '')[:60]
    elif tool_name == 'WebSearch':
        detail = (tool_input.get('query') or tool_input.get('search_term') or '')[:60]
    elif tool_name == 'Task':
        detail = (tool_input.get('description') or '')[:60]
    if detail:
        return f'{label}: {detail}'
    return f'{label} ...'
