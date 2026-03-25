from __future__ import annotations
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
import requests
from . import runtime as runtime_state
from .env import _collect_env_from_map, _collect_new_env, _load_project_env_map, _project_env_comments, _project_env_values, _write_env_value_lines
from .runtime import BOT_USERNAME_ENV, BootstrapResult, BotIdentity, CODE_DIR, PROJECTS_ROOT, _launch_script_path, _pm2_name_file, _pm2_name_for_instance, _sanitize_instance_name

def _bot_identity_from_env() -> str:
    raw = os.environ.get(runtime_state.BOT_USERNAME_ENV, '').strip()
    return _sanitize_instance_name(raw) if raw else ''


def _telegram_result_ok(data: Any) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return (False, str(data)[:300])
    if data.get('ok'):
        return (True, '')
    desc = str(data.get('description', data))
    return (False, desc)

def _resolve_bot_identity(bot_token: str) -> BotIdentity:
    token = (bot_token or '').strip()
    if not token:
        raise ValueError('Telegram bot token is required.')
    try:
        response = requests.get(f'https://api.telegram.org/bot{token}/getMe', timeout=15)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f'Failed to resolve Telegram bot identity: {type(exc).__name__}') from exc
    (ok, desc) = _telegram_result_ok(data)
    if not ok:
        raise RuntimeError(f'Telegram getMe failed: {desc[:160]}')
    result = data.get('result') or {}
    username = _sanitize_instance_name(str(result.get('username') or ''))
    display_name = str(result.get('first_name') or username)
    bot_id = result.get('id')
    try:
        bot_id = int(bot_id) if bot_id is not None else None
    except (TypeError, ValueError):
        bot_id = None
    return BotIdentity(username=username, bot_id=bot_id, display_name=display_name)

def _init_project_layout(project_dir: Path) -> None:
    workspace_dir = project_dir / 'workspace'
    for path in (project_dir / 'runs', project_dir / 'chat', project_dir / 'files', project_dir / 'logs', project_dir / '.claude', workspace_dir):
        path.mkdir(parents=True, exist_ok=True)
    for path in (project_dir / 'GOAL.md', project_dir / 'STATE.md', project_dir / 'INBOX.md'):
        path.touch(exist_ok=True)

def _seed_workspace_defaults(workspace_dir: Path) -> None:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    for name in ('agcli', 'taocli'):
        source = runtime_state.CODE_DIR / name
        if not source.exists():
            continue
        target = workspace_dir / name
        if target.exists():
            continue
        shutil.move(str(source), str(target))

def _copy_workspace_snapshot(source_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for child in source_dir.iterdir():
        dest = target_dir / child.name
        if dest.exists():
            continue
        if child.is_dir():
            shutil.copytree(child, dest, symlinks=True)
        else:
            shutil.copy2(child, dest)

def _write_launch_script(project_dir: Path) -> Path:
    launch_script = _launch_script_path(project_dir)
    launch_script.write_text('#!/usr/bin/env bash\nexport PATH="$HOME/.local/bin:$HOME/.cargo/bin:$HOME/.npm-global/bin:/usr/local/bin:$PATH"\nSCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\nARBOS_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"\ncd "$SCRIPT_DIR"\nset -a; [ -f ".env" ] && source ".env"; set +a\nsource "$ARBOS_ROOT/.venv/bin/activate"\nexec "$ARBOS_ROOT/.venv/bin/arbos" -p "$SCRIPT_DIR" 2>&1\n')
    os.chmod(launch_script, 493)
    return launch_script

def _pm2_bin() -> str | None:
    candidates = [shutil.which('pm2'), str(Path.home() / '.npm-global' / 'lib' / 'node_modules' / 'pm2' / 'bin' / 'pm2'), str(Path.home() / '.npm-global' / 'bin' / 'pm2'), str(Path.home() / '.local' / 'bin' / 'pm2')]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None

def _pm2_describe(name: str) -> bool:
    pm2_bin = _pm2_bin()
    if not pm2_bin:
        return False
    result = subprocess.run([pm2_bin, 'describe', name], capture_output=True, text=True, timeout=20)
    return result.returncode == 0

def _start_pm2_project(project_dir: Path, instance_name: str) -> str:
    pm2_bin = _pm2_bin()
    if not pm2_bin:
        raise RuntimeError('pm2 is not installed or not available on PATH.')
    pm2_name = _pm2_name_for_instance(instance_name)
    launch_script = _write_launch_script(project_dir)
    if _pm2_describe(pm2_name):
        subprocess.run([pm2_bin, 'delete', pm2_name], capture_output=True, text=True, timeout=30)
    subprocess.run([pm2_bin, 'start', str(launch_script), '--name', pm2_name, '--cwd', str(project_dir), '--log', str(project_dir / 'logs' / 'arbos.log'), '--time', '--restart-delay', '10000'], capture_output=True, text=True, timeout=30, check=True)
    subprocess.run([pm2_bin, 'save'], capture_output=True, text=True, timeout=30)
    _pm2_name_file(project_dir).write_text(pm2_name + '\n')
    return pm2_name

def _migrate_project_dir(source_dir: Path, *, bot_token: str, owner_id: str='', openrouter: str='', no_start: bool=False) -> BootstrapResult:
    env_values = _load_project_env_map(source_dir, token_hint=bot_token)
    owner = owner_id.strip() or env_values.get('TELEGRAM_OWNER_ID', '').strip()
    openrouter_key = openrouter.strip() or env_values.get('OPENROUTER_API_KEY', '').strip()
    identity = _resolve_bot_identity(bot_token)
    canonical_dir = runtime_state.PROJECTS_ROOT / identity.username
    if canonical_dir.exists() and canonical_dir != source_dir:
        raise FileExistsError(f'Target context already exists: {canonical_dir}')
    old_pm2_name = ''
    pm2_name_file = _pm2_name_file(source_dir)
    if pm2_name_file.exists():
        old_pm2_name = pm2_name_file.read_text().strip()
    if canonical_dir != source_dir:
        shutil.move(str(source_dir), str(canonical_dir))
    _init_project_layout(canonical_dir)
    extra_env = _collect_env_from_map(env_values)
    env_map = _project_env_values(canonical_dir, bot_token=bot_token, openrouter_api_key=openrouter_key, owner_id=owner, bot_username=identity.username, extra_env=extra_env)
    _write_env_value_lines(canonical_dir / '.env', env_map, _project_env_comments())
    new_pm2_name = _pm2_name_for_instance(identity.username)
    if old_pm2_name and old_pm2_name != new_pm2_name and _pm2_describe(old_pm2_name):
        pm2_bin = _pm2_bin()
        subprocess.run([pm2_bin, 'delete', old_pm2_name], capture_output=True, text=True, timeout=30)
    _write_launch_script(canonical_dir)
    _pm2_name_file(canonical_dir).write_text(new_pm2_name + '\n')
    if not no_start:
        new_pm2_name = _start_pm2_project(canonical_dir, identity.username)
    return BootstrapResult(identity=identity, project_dir=canonical_dir, env_file=canonical_dir / '.env', pm2_name=new_pm2_name or _pm2_name_for_instance(identity.username), launch_script=_launch_script_path(canonical_dir))

def _create_new_project(new_bot_token: str) -> BootstrapResult:
    token = (new_bot_token or '').strip()
    current_token = os.environ.get('TAU_BOT_TOKEN', '').strip()
    if not token:
        raise ValueError('Usage: /new <bot token>')
    if current_token and token == current_token:
        raise ValueError('New bot token must be different from the current bot token.')
    identity = _resolve_bot_identity(token)
    project_dir = runtime_state.PROJECTS_ROOT / identity.username
    if project_dir.exists():
        raise FileExistsError(f'Context already exists for @{identity.username}.')
    if _pm2_describe(_pm2_name_for_instance(identity.username)):
        raise FileExistsError(f'pm2 process already exists for @{identity.username}.')
    try:
        _init_project_layout(project_dir)
        extra_env = _collect_new_env()
        owner_id = os.environ.get('TELEGRAM_OWNER_ID', '').strip()
        env_map = _project_env_values(project_dir, bot_token=token, owner_id=owner_id, bot_username=identity.username, extra_env=extra_env)
        _write_env_value_lines(project_dir / '.env', env_map, _project_env_comments())
        pm2_name = _start_pm2_project(project_dir, identity.username)
        return BootstrapResult(identity=identity, project_dir=project_dir, env_file=project_dir / '.env', pm2_name=pm2_name, launch_script=_launch_script_path(project_dir))
    except Exception:
        if project_dir.exists():
            shutil.rmtree(project_dir, ignore_errors=True)
        raise
