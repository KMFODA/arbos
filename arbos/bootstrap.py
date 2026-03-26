from __future__ import annotations
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4
from cryptography.fernet import Fernet
import requests
from . import runtime as runtime_state
from .env import _apply_env_key_comment_block, _collect_env_from_map, _collect_new_env, _derive_fernet_key, _dotenv_double_quote_value, _load_project_env_map, _project_env_comments, _project_env_values, _write_env_value_lines
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

def _copy_project_snapshot(source_dir: Path, target_dir: Path, *, dirs_exist_ok: bool=False) -> None:
    """Copy the full project state except bot-specific launch/env files."""
    ignored = (
        '.env',
        '.env.enc',
        runtime_state.RESTART_FLAG.name,
        runtime_state.STEP_MSG_FILE.name,
        _launch_script_path(source_dir).name,
        _pm2_name_file(source_dir).name,
    )
    shutil.copytree(
        source_dir,
        target_dir,
        symlinks=True,
        ignore=shutil.ignore_patterns(*ignored),
        dirs_exist_ok=dirs_exist_ok,
    )

def _fork_staging_dir(instance_name: str) -> Path:
    return runtime_state.PROJECTS_ROOT / f'.fork-{instance_name}-{uuid4().hex[:8]}'

def _project_env_plaintext(project_dir: Path, *, bot_token: str) -> tuple[str, bool]:
    env_file = project_dir / '.env'
    if env_file.exists():
        return (env_file.read_text(), False)
    env_enc_file = project_dir / '.env.enc'
    if not env_enc_file.exists():
        return ('', False)
    token = bot_token.strip()
    if not token:
        raise ValueError('Current project TAU_BOT_TOKEN is required to rewrite an encrypted .env.enc.')
    fernet = Fernet(_derive_fernet_key(token))
    return (fernet.decrypt(env_enc_file.read_bytes()).decode(), True)

def _write_fork_env(source_dir: Path, target_dir: Path, *, new_bot_token: str, bot_username: str) -> Path:
    """Clone the source project env while swapping bot-specific identity fields."""
    current_token = os.environ.get('TAU_BOT_TOKEN', '').strip()
    (raw_env, encrypted) = _project_env_plaintext(source_dir, bot_token=current_token)
    lines = raw_env.splitlines() if raw_env else []
    env_map = _load_project_env_map(source_dir, token_hint=current_token)
    if not lines and env_map:
        for (key, value) in env_map.items():
            if not value:
                continue
            desc = _project_env_comments().get(key, key.replace('_', ' ').lower())
            value_line = f'{key}="{_dotenv_double_quote_value(value)}"'
            lines = _apply_env_key_comment_block(lines, key, value_line, desc)
    updates = {
        'TAU_BOT_TOKEN': new_bot_token,
        BOT_USERNAME_ENV: bot_username,
        'ARBOS_PROJECT': bot_username,
    }
    owner_id = env_map.get('TELEGRAM_OWNER_ID', '').strip()
    if owner_id:
        updates['TELEGRAM_OWNER_ID'] = owner_id
    comments = _project_env_comments()
    for (key, value) in updates.items():
        desc = comments.get(key, key.replace('_', ' ').lower())
        value_line = f'{key}="{_dotenv_double_quote_value(value)}"'
        lines = _apply_env_key_comment_block(lines, key, value_line, desc)
    env_path = target_dir / '.env'
    env_path.write_text('\n'.join(lines).rstrip() + '\n')
    os.chmod(env_path, 384)
    if encrypted:
        env_enc_path = target_dir / '.env.enc'
        fernet = Fernet(_derive_fernet_key(new_bot_token))
        env_enc_path.write_bytes(fernet.encrypt(env_path.read_bytes()))
        os.chmod(env_enc_path, 384)
        env_path.unlink()
        return env_enc_path
    return env_path

def _fork_target_incomplete(project_dir: Path) -> bool:
    """True when a target directory exists but never reached runnable state."""
    if not project_dir.exists():
        return False
    has_env = (project_dir / '.env').exists() or (project_dir / '.env.enc').exists()
    has_pm2_name = _pm2_name_file(project_dir).exists()
    has_launch = _launch_script_path(project_dir).exists()
    return (not has_env) or (not has_pm2_name) or (not has_launch)

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

def _pm2_jlist() -> list[dict[str, Any]]:
    pm2_bin = _pm2_bin()
    if not pm2_bin:
        return []
    try:
        result = subprocess.run([pm2_bin, 'jlist'], capture_output=True, text=True, timeout=20, check=True)
    except Exception:
        return []
    try:
        data = json.loads(result.stdout or '[]')
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []

def _migration_preflight(source_dir: Path, *, new_bot_token: str) -> dict[str, Any]:
    token = (new_bot_token or '').strip()
    current_name = source_dir.name
    current_token = os.environ.get('TAU_BOT_TOKEN', '').strip()
    if not current_token:
        env_values = _load_project_env_map(source_dir, token_hint='')
        current_token = str(env_values.get('TAU_BOT_TOKEN') or '').strip()
    current_pm2_name = _pm2_name_file(source_dir).read_text().strip() if _pm2_name_file(source_dir).exists() else _pm2_name_for_instance(current_name)
    target_identity = _resolve_bot_identity(token) if token else None
    target_dir = (runtime_state.PROJECTS_ROOT / target_identity.username) if target_identity else None
    companion_items: list[dict[str, str]] = []
    active_statuses = {'online', 'launching', 'waiting restart', 'waiting', 'stopping'}
    for item in _pm2_jlist():
        name = str(item.get('name') or '').strip()
        status = str((item.get('pm2_env') or {}).get('status') or '').strip()
        if (not name) or name == current_pm2_name or current_name not in name:
            continue
        companion_items.append({'name': name, 'status': status or 'unknown'})
    companion_items.sort(key=lambda item: item['name'])
    blocking = [item for item in companion_items if item['status'] in active_statuses]
    return {
        'source_project': current_name,
        'source_dir': str(source_dir),
        'current_pm2_name': current_pm2_name,
        'current_token_loaded': bool(current_token),
        'same_token': bool(token and current_token and token == current_token),
        'target_username': (target_identity.username if target_identity else ''),
        'target_dir': (str(target_dir) if target_dir else ''),
        'target_exists': bool(target_dir and target_dir.exists()),
        'companion_pm2': companion_items,
        'blocking_companion_pm2': blocking,
    }

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

def _migrate_current_project_to_token(source_dir: Path, *, new_bot_token: str, no_start: bool=False) -> BootstrapResult:
    token = (new_bot_token or '').strip()
    current_token = os.environ.get('TAU_BOT_TOKEN', '').strip()
    if not token:
        raise ValueError('Usage: /migrate <bot token>')
    if not current_token:
        env_values = _load_project_env_map(source_dir, token_hint='')
        current_token = str(env_values.get('TAU_BOT_TOKEN') or '').strip()
    if not current_token:
        raise ValueError('Current project TAU_BOT_TOKEN is not loaded.')
    preflight = _migration_preflight(source_dir, new_bot_token=token)
    blocking = preflight.get('blocking_companion_pm2') or []
    if blocking:
        names = ', '.join((item['name'] for item in blocking))
        raise RuntimeError(f'Cannot migrate while companion pm2 services are active: {names}')
    if token == current_token:
        raise ValueError('Migrate bot token must be different from the current bot token.')
    identity = _resolve_bot_identity(token)
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
    env_file = _write_fork_env(canonical_dir, canonical_dir, new_bot_token=token, bot_username=identity.username)
    new_pm2_name = _pm2_name_for_instance(identity.username)
    _write_launch_script(canonical_dir)
    _pm2_name_file(canonical_dir).write_text(new_pm2_name + '\n')
    if no_start:
        return BootstrapResult(identity=identity, project_dir=canonical_dir, env_file=env_file, pm2_name=new_pm2_name, launch_script=_launch_script_path(canonical_dir))
    new_pm2_name = _start_pm2_project(canonical_dir, identity.username)
    if old_pm2_name and old_pm2_name != new_pm2_name and _pm2_describe(old_pm2_name):
        pm2_bin = _pm2_bin()
        if pm2_bin:
            subprocess.Popen([pm2_bin, 'delete', old_pm2_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return BootstrapResult(identity=identity, project_dir=canonical_dir, env_file=env_file, pm2_name=new_pm2_name, launch_script=_launch_script_path(canonical_dir))

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

def _create_fork_project(new_bot_token: str) -> BootstrapResult:
    token = (new_bot_token or '').strip()
    current_token = os.environ.get('TAU_BOT_TOKEN', '').strip()
    if not token:
        raise ValueError('Usage: /fork <bot token>')
    if not current_token:
        raise ValueError('Current project TAU_BOT_TOKEN is not loaded.')
    if token == current_token:
        raise ValueError('Fork bot token must be different from the current bot token.')
    identity = _resolve_bot_identity(token)
    project_dir = runtime_state.PROJECTS_ROOT / identity.username
    target_preexists = project_dir.exists()
    if target_preexists and not _fork_target_incomplete(project_dir):
        raise FileExistsError(f'Context already exists for @{identity.username}.')
    if _pm2_describe(_pm2_name_for_instance(identity.username)):
        raise FileExistsError(f'pm2 process already exists for @{identity.username}.')
    if runtime_state.PROJECT_DIR.resolve() == project_dir.resolve():
        raise FileExistsError(f'Current project already uses @{identity.username}.')
    staging_dir = _fork_staging_dir(identity.username)
    promoted = False
    try:
        _copy_project_snapshot(runtime_state.PROJECT_DIR, staging_dir)
        _init_project_layout(staging_dir)
        staged_env_file = _write_fork_env(runtime_state.PROJECT_DIR, staging_dir, new_bot_token=token, bot_username=identity.username)
        if target_preexists:
            shutil.rmtree(project_dir, ignore_errors=True)
        staging_dir.rename(project_dir)
        promoted = True
        pm2_name = _start_pm2_project(project_dir, identity.username)
        env_file = project_dir / staged_env_file.name
        return BootstrapResult(identity=identity, project_dir=project_dir, env_file=env_file, pm2_name=pm2_name, launch_script=_launch_script_path(project_dir))
    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        if promoted and project_dir.exists():
            shutil.rmtree(project_dir, ignore_errors=True)
        raise
