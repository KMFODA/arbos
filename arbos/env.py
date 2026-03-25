from __future__ import annotations
import base64
import io
import os
import re
import sys
import threading
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from dotenv import dotenv_values, load_dotenv
from . import runtime as runtime_state

def _write_env_value_lines(path: Path, values: dict[str, str], comments: dict[str, str] | None=None) -> None:
    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text().splitlines()
    out = list(existing_lines)
    for (key, value) in values.items():
        desc = (comments or {}).get(key, key.replace('_', ' ').lower())
        value_line = f'{key}="{_dotenv_double_quote_value(value)}"'
        out = _apply_env_key_comment_block(out, key, value_line, desc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(out).rstrip() + '\n')
    os.chmod(path, 384)

def _load_project_env_map(project_dir: Path, *, token_hint: str='') -> dict[str, str]:
    env_file = project_dir / '.env'
    if env_file.exists():
        return {str(k): str(v).strip() for (k, v) in dotenv_values(env_file).items() if k and v is not None}
    env_enc = project_dir / '.env.enc'
    if env_enc.exists():
        token = token_hint.strip()
        if not token:
            return {}
        f = Fernet(_derive_fernet_key(token))
        try:
            content = f.decrypt(env_enc.read_bytes()).decode()
        except InvalidToken:
            return {}
        return {str(k): str(v).strip() for (k, v) in dotenv_values(stream=io.StringIO(content)).items() if k and v is not None}
    return {}

def _iter_context_dirs() -> list[Path]:
    if not runtime_state.PROJECTS_ROOT.exists():
        return []
    return sorted([path for path in runtime_state.PROJECTS_ROOT.iterdir() if path.is_dir() and (not path.name.startswith('.'))])

def _find_projects_for_token(bot_token: str) -> list[Path]:
    matches: list[Path] = []
    token = (bot_token or '').strip()
    if not token:
        return matches
    for project_dir in _iter_context_dirs():
        env_map = _load_project_env_map(project_dir, token_hint=token)
        if env_map.get('TAU_BOT_TOKEN', '').strip() == token:
            matches.append(project_dir)
    return matches

def _collect_env_from_map(env_map: dict[str, str]) -> dict[str, str]:
    shared: dict[str, str] = {}
    for key in runtime_state.FORK_SHARED_ENV_KEYS:
        value = env_map.get(key, '').strip()
        if value:
            shared[key] = value
    return shared

def _project_env_values(project_dir: Path, *, bot_token: str, openrouter_api_key: str='', owner_id: str='', bot_username: str='', extra_env: dict[str, str] | None=None) -> dict[str, str]:
    env_map: dict[str, str] = {}
    if extra_env:
        for (key, value) in extra_env.items():
            if value:
                env_map[key] = value
    if openrouter_api_key:
        env_map['OPENROUTER_API_KEY'] = openrouter_api_key
    env_map['TAU_BOT_TOKEN'] = bot_token
    if owner_id:
        env_map['TELEGRAM_OWNER_ID'] = owner_id
    if bot_username:
        env_map[runtime_state.BOT_USERNAME_ENV] = bot_username
        env_map['ARBOS_PROJECT'] = bot_username
    return env_map

def _project_env_comments() -> dict[str, str]:
    return {'OPENROUTER_API_KEY': 'OpenRouter API key', 'TAU_BOT_TOKEN': 'Telegram bot token for this instance', 'TELEGRAM_OWNER_ID': 'Telegram user id allowed to control this bot', runtime_state.BOT_USERNAME_ENV: 'Canonical Telegram bot username for this instance', 'ARBOS_PROJECT': 'Canonical Arbos project name for this instance', 'CLAUDE_MODEL': 'Claude model override', 'CLAUDE_MAX_RETRIES': 'Claude retry limit', 'CLAUDE_TIMEOUT': 'Claude idle timeout', 'AGENT_DELAY': 'Additional delay between loop steps'}

def _derive_fernet_key(passphrase: str) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b'arbos-env-v1', iterations=200000)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))

def _encrypt_env_file(bot_token: str):
    """Encrypt .env → .env.enc and delete the plaintext file."""
    plaintext = runtime_state.ENV_FILE.read_bytes()
    f = Fernet(_derive_fernet_key(bot_token))
    runtime_state.ENV_ENC_FILE.write_bytes(f.encrypt(plaintext))
    os.chmod(str(runtime_state.ENV_ENC_FILE), 384)
    runtime_state.ENV_FILE.unlink()

def _decrypt_env_content(bot_token: str) -> str:
    """Decrypt .env.enc and return plaintext (never written to disk)."""
    f = Fernet(_derive_fernet_key(bot_token))
    return f.decrypt(runtime_state.ENV_ENC_FILE.read_bytes()).decode()

def _load_encrypted_env(bot_token: str) -> bool:
    """Decrypt .env.enc, load into os.environ. Returns True on success."""
    if not runtime_state.ENV_ENC_FILE.exists():
        return False
    try:
        content = _decrypt_env_content(bot_token)
    except InvalidToken:
        return False
    for line in content.splitlines():
        line = line.split('#')[0].strip()
        if '=' not in line:
            continue
        (k, v) = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('\'"'))
    return True

def _save_to_encrypted_env(key: str, value: str):
    """Add/update a single key in the encrypted env file."""
    bot_token = os.environ.get('TAU_BOT_TOKEN', '')
    if not bot_token or not runtime_state.ENV_ENC_FILE.exists():
        return
    try:
        content = _decrypt_env_content(bot_token)
    except InvalidToken:
        return
    lines = content.splitlines()
    updated = False
    for (i, line) in enumerate(lines):
        stripped = line.split('#')[0].strip()
        if stripped.startswith(f'{key}='):
            lines[i] = f"{key}='{value}'"
            updated = True
            break
    if not updated:
        lines.append(f"{key}='{value}'")
    f = Fernet(_derive_fernet_key(bot_token))
    runtime_state.ENV_ENC_FILE.write_bytes(f.encrypt('\n'.join(lines).encode()))
    os.environ[key] = value

def _init_env():
    """Load environment from .env (plaintext) or .env.enc (encrypted)."""
    if runtime_state.ENV_FILE.exists():
        load_dotenv(runtime_state.ENV_FILE)
        return
    bot_token = os.environ.get('TAU_BOT_TOKEN', '')
    if runtime_state.ENV_ENC_FILE.exists() and bot_token:
        if _load_encrypted_env(bot_token):
            return
        print('ERROR: failed to decrypt .env.enc — wrong TAU_BOT_TOKEN?', file=sys.stderr)
        sys.exit(1)
    if runtime_state.ENV_ENC_FILE.exists() and (not bot_token):
        print('ERROR: .env.enc exists but TAU_BOT_TOKEN not set.', file=sys.stderr)
        print('Pass it as an env var: TAU_BOT_TOKEN=xxx arbos -p <project>', file=sys.stderr)
        sys.exit(1)

def _process_pending_env():
    """Pick up env vars the operator agent wrote to .env.pending and persist them."""
    with runtime_state._pending_env_lock:
        if not runtime_state.ENV_PENDING_FILE.exists():
            return
        content = runtime_state.ENV_PENDING_FILE.read_text().strip()
        runtime_state.ENV_PENDING_FILE.unlink(missing_ok=True)
        if not content:
            return
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            (k, v) = line.split('=', 1)
            (k, v) = (k.strip(), v.strip().strip('\'"'))
            os.environ[k] = v
        if runtime_state.ENV_FILE.exists():
            with open(runtime_state.ENV_FILE, 'a') as f:
                f.write('\n' + content + '\n')
        elif runtime_state.ENV_ENC_FILE.exists():
            bot_token = os.environ.get('TAU_BOT_TOKEN', '')
            if bot_token:
                try:
                    existing = _decrypt_env_content(bot_token)
                except InvalidToken:
                    existing = ''
                new_content = existing.rstrip() + '\n' + content + '\n'
                enc = Fernet(_derive_fernet_key(bot_token))
                runtime_state.ENV_ENC_FILE.write_bytes(enc.encrypt(new_content.encode()))
        _reload_env_secrets()
        from .logs import _log
        _log(f'loaded pending env vars from {runtime_state.ENV_PENDING_FILE}')
_SECRET_KEY_WORDS = {'KEY', 'SECRET', 'TOKEN', 'PASSWORD', 'SEED', 'CREDENTIAL'}
_SECRET_PATTERNS = [re.compile('sk-[a-zA-Z0-9_\\-]{20,}'), re.compile('sk_[a-zA-Z0-9_\\-]{20,}'), re.compile('sk-proj-[a-zA-Z0-9_\\-]{20,}'), re.compile('sk-or-v1-[a-fA-F0-9]{20,}'), re.compile('ghp_[a-zA-Z0-9]{20,}'), re.compile('gho_[a-zA-Z0-9]{20,}'), re.compile('hf_[a-zA-Z0-9]{20,}'), re.compile('AKIA[0-9A-Z]{16}'), re.compile('cpk_[a-zA-Z0-9._\\-]{20,}'), re.compile('crsr_[a-zA-Z0-9]{20,}'), re.compile('dckr_pat_[a-zA-Z0-9_\\-]{10,}'), re.compile('sn\\d+_[a-zA-Z0-9_]{10,}'), re.compile('tpn-[a-zA-Z0-9_\\-]{10,}'), re.compile('wandb_v\\d+_[a-zA-Z0-9]{10,}'), re.compile('basilica_[a-zA-Z0-9]{20,}'), re.compile('MT[A-Za-z0-9]+\\.[A-Za-z0-9_\\-]+\\.[A-Za-z0-9_\\-]{20,}')]

def _load_env_secrets() -> set[str]:
    """Build redaction blocklist from env vars whose names suggest secrets."""
    secrets = set()
    for (key, val) in os.environ.items():
        if len(val) < 16:
            continue
        key_upper = key.upper()
        if any((w in key_upper for w in _SECRET_KEY_WORDS)):
            secrets.add(val)
    return secrets
_env_secrets: set[str] = _load_env_secrets()

def _reload_env_secrets():
    global _env_secrets
    _env_secrets = _load_env_secrets()

def _redact_secrets(text: str) -> str:
    """Strip known secrets and common key patterns from outgoing text."""
    for secret in _env_secrets:
        if secret in text:
            text = text.replace(secret, '[REDACTED]')
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    return text
runtime_state._tls = threading.local()
runtime_state._log_lock = threading.Lock()
runtime_state._chatlog_lock = threading.Lock()
runtime_state._pending_env_lock = threading.Lock()
runtime_state._token_lock_fh = None
runtime_state._instance_lock_fh = None
_ENV_KEY_NAME_RE = re.compile('^[A-Za-z_][A-Za-z0-9_]*$')
_MAX_TELEGRAM_ENV_KEY_LEN = 128
_MAX_TELEGRAM_ENV_VALUE_LEN = 8192
_MAX_TELEGRAM_ENV_DESC_LEN = 500

def _sanitize_telegram_env_description(text: str) -> str:
    """Single-line comment text; no newlines or control characters."""
    t = ' '.join((text or '').split())
    t = ''.join((ch for ch in t if ch >= ' ' or ch in '\t'))
    t = t.replace('\t', ' ')
    return t.strip()[:_MAX_TELEGRAM_ENV_DESC_LEN].strip()

def _dotenv_double_quote_value(val: str) -> str:
    if len(val) > _MAX_TELEGRAM_ENV_VALUE_LEN:
        raise ValueError(f'value exceeds {_MAX_TELEGRAM_ENV_VALUE_LEN} characters')
    if '\n' in val or '\r' in val:
        raise ValueError('value must not contain line breaks')
    return val.replace('\\', '\\\\').replace('"', '\\"')

def _apply_env_key_comment_block(lines: list[str], key: str, value_line: str, comment_text: str) -> list[str]:
    """Insert or update ``# comment`` + ``KEY=...`` block in .env-style lines."""
    assign_prefix = f'{key}='
    idx = None
    for (i, line) in enumerate(lines):
        stripped = line.split('#', 1)[0].strip()
        if stripped.startswith(assign_prefix):
            idx = i
            break
    comment_line = f'# {comment_text}'.rstrip()
    if idx is None:
        out = list(lines)
        if out and out[-1].strip():
            out.append('')
        out.append(comment_line)
        out.append(value_line)
        return out
    out = list(lines)
    if idx > 0 and out[idx - 1].strip().startswith('#'):
        out[idx - 1] = comment_line
        out[idx] = value_line
    else:
        out.insert(idx, comment_line)
        out[idx + 1] = value_line
    return out

def _persist_env_var_with_comment(key: str, value: str, description: str) -> tuple[bool, str]:
    """Append or update a variable in ``.env`` / ``.env.enc`` with a preceding comment line."""
    key = key.strip()
    if not key or len(key) > _MAX_TELEGRAM_ENV_KEY_LEN or (not _ENV_KEY_NAME_RE.match(key)):
        return (False, 'Invalid KEY: use letters, digits, underscore; must start with letter or _.')
    desc = _sanitize_telegram_env_description(description)
    if not desc:
        return (False, 'DESCRIPTION required (non-empty after trimming).')
    try:
        escaped = _dotenv_double_quote_value(value)
    except ValueError as e:
        return (False, str(e))
    value_line = f'{key}="{escaped}"'
    env_path = runtime_state.ENV_FILE
    with runtime_state._pending_env_lock:
        if env_path.exists():
            content = env_path.read_text()
            new_lines = _apply_env_key_comment_block(content.splitlines(), key, value_line, desc)
            env_path.write_text('\n'.join(new_lines) + '\n')
        elif runtime_state.ENV_ENC_FILE.exists():
            bot_token = os.environ.get('TAU_BOT_TOKEN', '')
            if not bot_token:
                return (False, 'Cannot update encrypted .env: TAU_BOT_TOKEN not set in this process.')
            try:
                content = _decrypt_env_content(bot_token)
            except InvalidToken:
                return (False, 'Could not decrypt .env.enc (wrong token?).')
            new_lines = _apply_env_key_comment_block(content.splitlines(), key, value_line, desc)
            payload = '\n'.join(new_lines) + '\n'
            fernet = Fernet(_derive_fernet_key(bot_token))
            runtime_state.ENV_ENC_FILE.write_bytes(fernet.encrypt(payload.encode()))
            os.chmod(str(runtime_state.ENV_ENC_FILE), 384)
        else:
            return (False, 'No .env or .env.enc in project directory.')
        os.environ[key] = value
        _reload_env_secrets()
    return (True, f'Saved `{key}` with comment (value not shown). Active in this process.')

def _collect_new_env() -> dict[str, str]:
    env_map: dict[str, str] = {}
    for key in runtime_state.FORK_SHARED_ENV_KEYS:
        value = os.environ.get(key, '').strip()
        if value:
            env_map[key] = value
    return env_map
