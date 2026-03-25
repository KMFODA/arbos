from __future__ import annotations
import argparse
import json
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from . import runtime as runtime_state
from .bootstrap import _copy_workspace_snapshot, _init_project_layout, _migrate_project_dir, _resolve_bot_identity, _seed_workspace_defaults, _start_pm2_project
from .claude import _write_claude_settings
from .env import _encrypt_env_file, _find_projects_for_token, _init_env, _load_project_env_map, _process_pending_env, _project_env_comments, _project_env_values, _reload_env_secrets, _write_env_value_lines
from .logs import _log
from .loop import _agent_manager, _clear_agent_runtime_history, _ensure_agent_thread, _kill_child_procs, _kill_registered_claude_procs, _kill_stale_claude_procs, _pre_send_normal_step_bubble, run_step
from .runtime import _acquire_runtime_singleton_locks, _apply_instance_paths, _build_instance_paths, _operator_set, _reload_runtime_config, _validate_runtime_identity
from .state import _load_agent, _reset_claude_invocations, log_chat
from .telegram import TELEGRAM_HELP_TEXT, _download_telegram_file, _edit_telegram_text, _is_leading_slash_command, _save_operator_telegram, _send_telegram_document, _send_telegram_new, _send_telegram_photo, _send_telegram_text, _truncate_telegram_text, run_bot

def _send_cli(args: list[str]):
    """CLI entry point: arbos -p <project> send 'message' [--file path]

    Within a step, all sends are consolidated into a single Telegram message.
    The first send creates it; subsequent sends edit it by appending.
    Uses the active project's .step_msg file for the active step bubble.
    """
    parser = argparse.ArgumentParser(description='Send a Telegram message to the operator')
    parser.add_argument('message', nargs='?', help='Message text to send')
    parser.add_argument('--file', help='Send contents of a file instead')
    parsed = parser.parse_args(args)
    if not parsed.message and (not parsed.file):
        parser.error('Provide a message or --file')
    if parsed.file:
        text = Path(parsed.file).read_text()
    else:
        text = parsed.message
    smf = runtime_state.STEP_MSG_FILE
    smf.parent.mkdir(parents=True, exist_ok=True)
    current_stream_id = os.environ.get(runtime_state.ARBOS_STEP_STREAM_ID_ENV, '').strip()
    if smf.exists():
        try:
            state = json.loads(smf.read_text())
            msg_id = state['msg_id']
            prev_text = state.get('text', '')
            owner_stream_id = state.get('stream_id', '')
        except (json.JSONDecodeError, KeyError):
            msg_id = None
            prev_text = ''
            owner_stream_id = ''
    else:
        msg_id = None
        prev_text = ''
        owner_stream_id = ''
    owns_step_message = bool(current_stream_id and (not owner_stream_id or owner_stream_id == current_stream_id))
    if msg_id and owns_step_message:
        combined = (prev_text + '\n\n' + text).strip()
        if _edit_telegram_text(msg_id, combined):
            smf.write_text(json.dumps({'msg_id': msg_id, 'text': combined, 'stream_id': current_stream_id}))
            log_chat('bot', combined[:1000])
            print(f'Edited step message ({len(combined)} chars)')
        else:
            new_id = _send_telegram_new(text)
            if new_id:
                smf.write_text(json.dumps({'msg_id': new_id, 'text': text, 'stream_id': current_stream_id}))
                log_chat('bot', text[:1000])
                print(f'Sent new message ({len(text)} chars)')
            else:
                print('Failed to send', file=sys.stderr)
                sys.exit(1)
    else:
        new_id = _send_telegram_new(text)
        if new_id:
            log_chat('bot', text[:1000])
            print(f'Sent ({len(text)} chars)')
        else:
            print(f'Failed to send (check TAU_BOT_TOKEN and {runtime_state.CHAT_ID_FILE})', file=sys.stderr)
            sys.exit(1)

def _sendfile_cli(args: list[str]):
    """CLI entry point: arbos -p <project> sendfile path/to/file [--caption 'text'] [--photo]"""
    parser = argparse.ArgumentParser(description='Send a file to the operator via Telegram')
    parser.add_argument('path', help='Path to the file to send')
    parser.add_argument('--caption', default='', help='Caption for the file')
    parser.add_argument('--photo', action='store_true', help='Send as a compressed photo instead of a document')
    parsed = parser.parse_args(args)
    file_path = Path(parsed.path)
    if not file_path.exists():
        print(f'File not found: {file_path}', file=sys.stderr)
        sys.exit(1)
    if parsed.photo:
        ok = _send_telegram_photo(str(file_path), caption=parsed.caption)
    else:
        ok = _send_telegram_document(str(file_path), caption=parsed.caption)
    if ok:
        print(f"Sent {('photo' if parsed.photo else 'file')}: {file_path.name}")
    else:
        print(f'Failed to send (check TAU_BOT_TOKEN and {runtime_state.CHAT_ID_FILE})', file=sys.stderr)
        sys.exit(1)

def _bot_name_cli(args: list[str]) -> None:
    parser = argparse.ArgumentParser(description='Resolve a Telegram bot username from a token')
    parser.add_argument('--bot-token', default=os.environ.get('TAU_BOT_TOKEN', ''))
    parsed = parser.parse_args(args)
    if not parsed.bot_token.strip():
        print('bot token is required', file=sys.stderr)
        sys.exit(1)
    identity = _resolve_bot_identity(parsed.bot_token)
    print(identity.username)

def _bootstrap_project_cli(args: list[str]) -> None:
    parser = argparse.ArgumentParser(description='Create or update a bot-named Arbos project')
    parser.add_argument('--bot-token', default=os.environ.get('TAU_BOT_TOKEN', ''))
    parser.add_argument('--openrouter-api-key', default=os.environ.get('OPENROUTER_API_KEY', ''))
    parser.add_argument('--owner-id', default=os.environ.get('TELEGRAM_OWNER_ID', ''))
    parser.add_argument('--copy-workspace-from', default='')
    parser.add_argument('--no-start', action='store_true')
    parsed = parser.parse_args(args)
    token = parsed.bot_token.strip()
    if not token:
        print('bot token is required', file=sys.stderr)
        sys.exit(1)
    identity = _resolve_bot_identity(token)
    project_dir = runtime_state.PROJECTS_ROOT / identity.username
    legacy_matches = [path for path in _find_projects_for_token(token) if path != project_dir]
    if not project_dir.exists() and len(legacy_matches) == 1:
        result = _migrate_project_dir(legacy_matches[0], bot_token=token, owner_id=parsed.owner_id.strip(), openrouter=parsed.openrouter_api_key.strip(), no_start=parsed.no_start)
        print(f'instance={result.identity.username}')
        print(f'context={result.project_dir}')
        print(f"workspace={result.project_dir / 'workspace'}")
        print(f'pm2={result.pm2_name}')
        print(f'migrated_from={legacy_matches[0]}')
        return
    if len(legacy_matches) > 1:
        print('Multiple legacy contexts use this bot token; migrate manually with `arbos -p <project> migrate-bot-names`.', file=sys.stderr)
        sys.exit(1)
    project_preexisted = project_dir.exists()
    try:
        _init_project_layout(project_dir)
        workspace_dir = project_dir / 'workspace'
        if parsed.copy_workspace_from.strip():
            _copy_workspace_snapshot(Path(parsed.copy_workspace_from).expanduser().resolve(), workspace_dir)
        else:
            _seed_workspace_defaults(workspace_dir)
        extra_env = {key: os.environ.get(key, '').strip() for key in runtime_state.FORK_SHARED_ENV_KEYS}
        env_map = _project_env_values(project_dir, bot_token=token, openrouter_api_key=parsed.openrouter_api_key.strip(), owner_id=parsed.owner_id.strip(), bot_username=identity.username, extra_env=extra_env)
        _write_env_value_lines(project_dir / '.env', env_map, _project_env_comments())
        pm2_name = ''
        if not parsed.no_start:
            pm2_name = _start_pm2_project(project_dir, identity.username)
        print(f'instance={identity.username}')
        print(f'context={project_dir}')
        print(f'workspace={workspace_dir}')
        if pm2_name:
            print(f'pm2={pm2_name}')
    except Exception:
        if not project_preexisted and project_dir.exists():
            shutil.rmtree(project_dir, ignore_errors=True)
        raise

def _migrate_bot_names_cli(project_arg: str | None, args: list[str]) -> None:
    parser = argparse.ArgumentParser(description='Rename a project context to its canonical Telegram bot username')
    parser.add_argument('--no-start', action='store_true')
    parsed = parser.parse_args(args)
    paths = _build_instance_paths(project_arg)
    project_dir = paths.project_dir
    env_values = _load_project_env_map(project_dir, token_hint=os.environ.get('TAU_BOT_TOKEN', ''))
    token = str(env_values.get('TAU_BOT_TOKEN') or os.environ.get('TAU_BOT_TOKEN', '')).strip()
    if not token:
        print('TAU_BOT_TOKEN is required for migration', file=sys.stderr)
        sys.exit(1)
    owner_id = str(env_values.get('TELEGRAM_OWNER_ID') or os.environ.get('TELEGRAM_OWNER_ID', '')).strip()
    openrouter = str(env_values.get('OPENROUTER_API_KEY') or os.environ.get('OPENROUTER_API_KEY', '')).strip()
    result = _migrate_project_dir(project_dir, bot_token=token, owner_id=owner_id, openrouter=openrouter, no_start=parsed.no_start)
    print(f'instance={result.identity.username}')
    print(f'context={result.project_dir}')
    print(f'pm2={result.pm2_name}')

def _parse_global_cli(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('-p', '--project', default=os.environ.get('ARBOS_PROJECT') or 'default', help='Project name or path. Simple names resolve under context/<project>.')
    return parser.parse_known_args(argv)

def _configure_runtime(project_arg: str | None) -> None:
    _apply_instance_paths(_build_instance_paths(project_arg))
    runtime_state.PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    runtime_state.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    os.environ['ARBOS_PROJECT'] = runtime_state.PROJECT_NAME
    os.environ['ARBOS_PROJECT_DIR'] = str(runtime_state.PROJECT_DIR)
    os.environ['ARBOS_CONTEXT_DIR'] = str(runtime_state.CONTEXT_DIR)
    os.environ['ARBOS_WORKSPACE_DIR'] = str(runtime_state.WORKSPACE_DIR)
    _init_env()
    _validate_runtime_identity()
    _reload_runtime_config()
    _reload_env_secrets()
    _acquire_runtime_singleton_locks()

def main() -> None:
    (global_args, remaining) = _parse_global_cli(sys.argv[1:])
    if remaining and remaining[0] == 'bot-name':
        _bot_name_cli(remaining[1:])
        return
    if remaining and remaining[0] == 'bootstrap-project':
        _bootstrap_project_cli(remaining[1:])
        return
    if remaining and remaining[0] == 'migrate-bot-names':
        _migrate_bot_names_cli(global_args.project, remaining[1:])
        return
    _configure_runtime(global_args.project)
    if remaining and remaining[0] == 'send':
        _send_cli(remaining[1:])
        return
    if remaining and remaining[0] == 'sendfile':
        _sendfile_cli(remaining[1:])
        return
    if remaining and remaining[0] == 'encrypt':
        if not runtime_state.ENV_FILE.exists():
            if runtime_state.ENV_ENC_FILE.exists():
                print('.env.enc already exists (already encrypted)')
            else:
                print('.env not found, nothing to encrypt')
            return
        bot_token = os.environ.get('TAU_BOT_TOKEN', '')
        if not bot_token:
            print('TAU_BOT_TOKEN must be set in .env', file=sys.stderr)
            sys.exit(1)
        _encrypt_env_file(bot_token)
        print('Encrypted .env → .env.enc, deleted plaintext.')
        print(f"On future starts: TAU_BOT_TOKEN='{bot_token}' arbos -p {runtime_state.PROJECT_NAME}")
        return
    if remaining:
        print(f'Unknown subcommand: {remaining[0]}', file=sys.stderr)
        print('Usage: arbos [-p PROJECT] [bot-name|bootstrap-project|migrate-bot-names|send|sendfile|encrypt]', file=sys.stderr)
        sys.exit(1)
    runtime_state._arbos_boot_wall = time.time()
    _operator_set('supervising', 'Arbos starting (health HTTP, agent loop, Telegram)')
    _log(f'arbos starting for project={runtime_state.PROJECT_NAME} dir={runtime_state.PROJECT_DIR} workspace={runtime_state.WORKSPACE_DIR} (openrouter, model={runtime_state.CLAUDE_MODEL})')
    _kill_stale_claude_procs()
    runtime_state.CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    runtime_state.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    runtime_state.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    runtime_state.CONTEXT_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    runtime_state.CHATLOG_DIR.mkdir(parents=True, exist_ok=True)
    runtime_state.FILES_DIR.mkdir(parents=True, exist_ok=True)
    _reset_claude_invocations()
    _load_agent()
    _log('loaded agent state from meta.json (if present)')
    if not runtime_state.LLM_API_KEY:
        _log('WARNING: OPENROUTER_API_KEY not set — LLM calls will fail')

    def _handle_sigterm(signum, frame):
        _log('SIGTERM received; shutting down gracefully')
        runtime_state._shutdown.set()
    signal.signal(signal.SIGTERM, _handle_sigterm)
    _write_claude_settings()
    _send_telegram_text(_truncate_telegram_text(TELEGRAM_HELP_TEXT.strip()))
    threading.Thread(target=_agent_manager, daemon=True).start()
    threading.Thread(target=run_bot, daemon=True).start()
    while not runtime_state._shutdown.is_set():
        if runtime_state.RESTART_FLAG.exists():
            runtime_state.RESTART_FLAG.unlink()
            _log('restart requested; killing children and exiting for pm2')
            _kill_child_procs()
            sys.exit(0)
        _process_pending_env()
        runtime_state._shutdown.wait(timeout=1)
    _log('shutdown: killing children')
    _kill_child_procs()
    _log('shutdown complete')
    sys.exit(0)
if __name__ == '__main__':
    main()
