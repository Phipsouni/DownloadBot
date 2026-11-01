"""Read-only publication check. Never print a secret, line content, or private ID.

Run before publishing: python security_check.py
Only the explicit source manifest is suitable for manual ZIP/upload.
"""
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT_FILES = (
    '.gitignore', 'README.md', 'R2-SETUP.md', 'SECURITY.md', 'Requirements.txt',
    'Install Requirments.bat', 'Start DL-06.bat', 'DL-06 logo.jpg', 'config.example.py',
    'r2_config.example.json', 'cloudflare_analytics.example.json',
    'DL-06.py', 'access_safety.py', 'admin_reports.py', 'audio_formats.py',
    'cloudflare_analytics.py', 'cookie_scope.py', 'cover_art.py',
    'download_pipeline.py', 'link_bundle.py', 'media_policy.py', 'music_sources.py',
    'network_safety.py', 'r2_admin.py', 'r2_storage.py', 'request_choices.py',
    'secret_safety.py', 'security_check.py', 'soundcloud_fallback.py',
    'stage_progress.py', 'traffic_stats.py',
)


def publication_files(root):
    root = Path(root)
    files = [root / name for name in ROOT_FILES if (root / name).is_file()]
    files.extend(sorted((root / 'tests').rglob('*.py')))
    return files


def local_secrets(root):
    root = Path(root)
    values = set()
    try:
        tree = ast.parse((root / 'config.py').read_text(encoding='utf-8-sig'))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'TOKEN' for target in node.targets):
                value = ast.literal_eval(node.value)
                if isinstance(value, str):
                    values.add(value)
    except (OSError, ValueError, SyntaxError):
        pass
    try:
        values.add((root / 'Token').read_text(encoding='utf-8-sig').strip())
    except OSError:
        pass
    for name in ('r2_config.json', 'cloudflare_analytics.json'):
        try:
            for key, value in json.loads((root / name).read_text(encoding='utf-8-sig')).items():
                if isinstance(value, str) and re.search(r'secret|token|access_key', key, re.I):
                    values.add(value)
        except (OSError, ValueError, AttributeError):
            pass
    for file in [root / 'cookies.txt', *(root / 'site_cookies').glob('*.txt')]:
        try:
            for line in file.read_text(encoding='utf-8-sig').splitlines():
                parts = line.removeprefix('#HttpOnly_').split('\t')
                if len(parts) == 7 and (not line.startswith('#') or line.startswith('#HttpOnly_')):
                    values.add(parts[6])
        except (OSError, ValueError):
            pass
    return {value for value in values if len(value) >= 8}


def scan_file(file, secrets):
    try:
        body = Path(file).read_text(encoding='utf-8-sig')
    except (UnicodeError, OSError):
        return []
    return scan_text(body, secrets)


def scan_text(body, secrets):
    findings = []
    for number, line in enumerate(body.splitlines(), 1):
        if any(secret in line for secret in secrets):
            findings.append((number, 'local credential'))
        elif re.search(r'\d{5,}:[A-Za-z0-9_-]{25,}', line):
            findings.append((number, 'Telegram token'))
        elif re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', line):
            findings.append((number, 'private key'))
        elif len(line.removeprefix('#HttpOnly_').split('\t')) == 7 and re.match(r'^#HttpOnly_|^[.#\w-]+\t(?:TRUE|FALSE)\t', line):
            findings.append((number, 'browser cookie record'))
        elif re.search(r'https?://\S+[?&]X-Amz-(?:Signature|Credential)=', line, re.I):
            findings.append((number, 'signed download URL'))
    return findings


def check(root):
    root = Path(root).resolve()
    public = publication_files(root)
    allowed = {file.relative_to(root).as_posix() for file in public}
    errors = []
    secrets = local_secrets(root)
    # Once Git exists, also inspect the index. .gitignore never untracks a file.
    if (root / '.git').exists():
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '-z'],
                                capture_output=True, check=True)
        for name in result.stdout.decode('utf-8').split('\0'):
            if name and name not in allowed:
                errors.append((name, 0, 'tracked file outside publication manifest'))
            elif name:
                staged = subprocess.run(['git', '-C', str(root), 'show', ':' + name],
                                        capture_output=True, check=True)
                try:
                    body = staged.stdout.decode('utf-8-sig')
                except UnicodeError:
                    continue
                for line, category in scan_text(body, secrets):
                    errors.append((name, line, 'Git index: ' + category))
    for file in public:
        for line, category in scan_file(file, secrets):
            errors.append((file.relative_to(root).as_posix(), line, category))
    return public, errors


if __name__ == '__main__':
    try:
        public, errors = check(Path(__file__).resolve().parent)
        for name, line, category in errors:
            print(f'BLOCKED: {name}:{line}: {category}')
        print(f'Publication check: {len(public)} source files; {len(errors)} findings.')
        print('Only source manifest checked. Do not upload the whole working folder or private backups.')
        sys.exit(bool(errors))
    except Exception:
        print('Publication check failed; publishing is blocked until the check can complete.')
        sys.exit(2)
