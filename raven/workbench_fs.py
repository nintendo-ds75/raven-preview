"""Private repository copies and network-isolated commands for browser tasks.

No model-selected command runs in Raven's server process. Docker receives only
the task's files, never the inference key, database, source checkout or socket.
"""
import base64
import fnmatch
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import uuid

from .store import Invalid

MAX_FILE = 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
MAX_FILES = 4000
IGNORED = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'node_modules'}


def safe_path(root, name):
    # A NUL byte is refused here: the filesystem calls below raise ValueError
    # on it, which the file-tool loop does not treat as a refusal.
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise Invalid('Use a relative repository path')
    parts = PurePosixPath(name)
    if parts.is_absolute() or '..' in parts.parts or any(p.casefold() == '.git' for p in parts.parts):
        raise Invalid('Path must stay inside the task copy and outside .git')
    base = Path(root)
    path = base.joinpath(*parts.parts)
    # Only the components below the task copy are checked for symlinks: the
    # copy itself may sit under a symlinked directory (macOS /tmp, /var) and
    # that is not an escape from it.
    inside = [path, *path.parents][:len(parts.parts)]
    if not path.resolve().is_relative_to(base.resolve()) or any(p.is_symlink() for p in inside):
        raise Invalid('Symlinks and paths outside the task copy are not allowed')
    return path


def git(root, *args):
    # Do not inherit operator git helpers, hooks, credentials or external diff.
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(root),
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
    result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'safe.directory=' + str(Path(root).resolve()), '-C', str(root), *args],
                            capture_output=True, timeout=60, env=env)
    if result.returncode:
        raise Invalid('The repository could not be read by git')
    return result.stdout


def snapshot(source, target):
    """Snapshot committed HEAD only; refuse symlinks/submodules and large copies."""
    revision = git(source, 'rev-parse', 'HEAD').decode().strip()
    listing = git(source, 'ls-tree', '-r', '-l', '-z', revision).decode().split('\0')
    sizes = []
    for item in filter(None, listing):
        metadata, _ = item.split('\t', 1)
        mode, kind, _, size = metadata.split()
        if kind != 'blob' or mode == '120000':
            raise Invalid('Browser task snapshots do not support symlinks or submodules')
        sizes.append(int(size))
    if len(sizes) > MAX_FILES or sum(sizes) > MAX_TOTAL or max(sizes, default=0) > MAX_FILE:
        raise Invalid('Repository exceeds browser-task size limits')
    archive = git(source, 'archive', '--format=tar', revision)
    if len(archive) > MAX_TOTAL * 2:
        raise Invalid('Repository archive is too large; use a smaller or sparse project')
    target = Path(target)
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    total = count = 0
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for entry in tar:
            if entry.isdir():
                continue
            if not entry.isfile():
                raise Invalid('Browser task snapshots do not support symlinks or submodules')
            total += entry.size
            count += 1
            if entry.size > MAX_FILE or total > MAX_TOTAL or count > MAX_FILES:
                raise Invalid('Repository exceeds browser-task size limits')
            path = safe_path(target, entry.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(tar.extractfile(entry).read())
            path.chmod(0o755 if entry.mode & 0o111 else 0o644)
    git(target, 'init', '--template=', '-b', 'main')
    # HEAD can contain deliberately tracked files that its .gitignore excludes.
    # Preserve them in the baseline so their later edits appear in the patch.
    git(target, 'add', '-f', '.')
    git(target, '-c', 'user.name=Raven', '-c', 'user.email=agent@example.invalid', 'commit', '-qm', 'Task baseline')
    return revision


def files(root):
    found = []
    for path in sorted(Path(root).rglob('*')):
        rel = path.relative_to(root)
        if any(p in IGNORED for p in rel.parts) or not path.is_file():
            continue
        safe_path(root, rel.as_posix())
        found.append(rel.as_posix())
        if len(found) > MAX_FILES:
            raise Invalid('Task copy has too many files')
    return found


def diff(root):
    git(root, 'add', '-N', '--', '.')
    return git(root, 'diff', '--no-ext-diff', '--no-textconv', 'HEAD', '--').decode('utf-8', 'replace')


def file_tool(root, name, args):
    if name == 'list_files':
        matches = [p for p in files(root) if fnmatch.fnmatch(p, str(args.get('pattern') or '*'))]
        return {'files': matches[:300], 'total': len(matches), 'more': len(matches) > 300}
    if name == 'search_files':
        text = args.get('text', '')
        if not isinstance(text, str) or not text:
            raise Invalid('A nonempty literal search string is required')
        matches = []
        for rel in files(root):
            path = safe_path(root, rel)
            if path.stat().st_size > MAX_FILE:
                continue
            for number, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
                if text.casefold() in line.casefold():
                    matches.append({'path': rel, 'line': number, 'text': line[:800]})
                    if len(matches) == 100:
                        return {'matches': matches, 'more': True}
        return {'matches': matches, 'more': False}
    path = safe_path(root, args.get('path'))
    if name == 'read_file':
        if path.stat().st_size > MAX_FILE:
            raise Invalid('File exceeds the read limit')
        lines = path.read_text().splitlines()
        start = max(1, int(args.get('start') or 1))
        limit = min(200, max(1, int(args.get('lines') or 120)))
        return {'path': args['path'], 'total_lines': len(lines),
                'content': '\n'.join(f'{i + start}: {line}' for i, line in enumerate(lines[start-1:start-1+limit]))}
    if name == 'write_file':
        content = args.get('content')
        if not isinstance(content, str) or len(content.encode()) > MAX_FILE:
            raise Invalid('File content must be text under 1 MiB')
        # Compare-and-write prevents overwriting a file the agent did not read.
        previous = args.get('previous')
        current = path.read_text() if path.exists() else None
        if previous != current:
            raise Invalid('File changed or previous content is missing; reread and supply exact previous content (null for a new file)')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return {'written': args['path'], 'bytes': len(content.encode())}
    if name == 'edit_file':
        old, new = args.get('old'), args.get('new')
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise Invalid('Supply nonempty old text and replacement text')
        content = path.read_text()
        if content.count(old) != 1:
            raise Invalid('Old text must match exactly once; reread the file')
        content = content.replace(old, new, 1)
        if len(content.encode()) > MAX_FILE:
            raise Invalid('Edited file exceeds the size limit')
        path.write_text(content)
        return {'edited': args['path']}
    raise Invalid('Unknown file tool')


# Runs INSIDE an ephemeral container, with no network and no host mounts.
SANDBOX_SCRIPT = r'''
import base64,json,os,signal,subprocess,sys,tempfile
from pathlib import Path
x=json.load(sys.stdin); root=Path('/workspace'); root.mkdir(exist_ok=True)
for name,value in x['files'].items():
 p=root/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(base64.b64decode(value['data'])); p.chmod(value['mode'])
with tempfile.TemporaryFile() as output:
 p=subprocess.Popen(x['command'],shell=True,cwd=root,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
 timed_out=False
 try: code=p.wait(timeout=120)
 except subprocess.TimeoutExpired:
  os.killpg(p.pid,signal.SIGKILL); p.wait(); code=124; timed_out=True
 output.seek(0,2); size=output.tell(); output.seek(max(0,size-64000)); text=output.read().decode('utf-8','replace')
result={}; total=0
for p in root.rglob('*'):
 rel=p.relative_to(root)
 if any(s in x['ignored'] for s in rel.parts) or not p.is_file(): continue
 if p.is_symlink() or not p.resolve().is_relative_to(root): continue
 n=p.stat().st_size; total+=n
 if n>1048576 or total>16777216 or len(result)>=4000: raise RuntimeError('Sandbox output exceeds limits')
 result[rel.as_posix()]={'data':base64.b64encode(p.read_bytes()).decode(),'mode':493 if p.stat().st_mode & 73 else 420}
print(json.dumps({'exit_code':code,'timed_out':timed_out,'output':('[earlier output omitted]\n' if size>64000 else '')+text,'files':result}))
'''


class DockerSandbox:
    def __init__(self, image='python:3.13-slim-bookworm'):
        self.image = image

    def run(self, root, command):
        if not isinstance(command, str) or not command.strip() or len(command) > 8000:
            raise Invalid('A command of up to 8,000 characters is required')
        payload = {}
        total = 0
        for rel in files(root):
            p = safe_path(root, rel)
            content = p.read_bytes()
            total += len(content)
            if len(content) > MAX_FILE or total > MAX_TOTAL:
                raise Invalid('Task copy exceeds sandbox limits')
            payload[rel] = {'data': base64.b64encode(content).decode(), 'mode': 0o755 if p.stat().st_mode & 0o111 else 0o644}
        name = 'raven-task-' + uuid.uuid4().hex
        cmd = ['docker', 'run', '--rm', '--pull=never', '--name', name, '-i', '--network=none',
               '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
               '--pids-limit=128', '--memory=1g', '--cpus=1', '--user=65534:65534',
               '--tmpfs=/workspace:rw,uid=65534,gid=65534,size=128m',
               '--tmpfs=/tmp:rw,uid=65534,gid=65534,size=128m', '-e', 'HOME=/tmp',
               '--entrypoint=python', self.image, '-c', SANDBOX_SCRIPT]
        try:
            response = subprocess.run(cmd, input=json.dumps({'files': payload, 'command': command, 'ignored': sorted(IGNORED)}),
                                      text=True, capture_output=True, timeout=150)
            if response.returncode:
                raise Invalid('Isolated tests could not run. Check Docker and the configured test image; no command ran on the server.')
            result = json.loads(response.stdout)
            returned = result.pop('files')
            # Validate the entire output before changing the private task copy.
            decoded = []
            for rel, value in returned.items():
                path = safe_path(root, rel)
                content = base64.b64decode(value['data'], validate=True)
                if len(content) > MAX_FILE:
                    raise Invalid('Sandbox returned an oversized file')
                decoded.append((path, content, value['mode']))
            if len(decoded) > MAX_FILES or sum(len(c) for _, c, _ in decoded) > MAX_TOTAL:
                raise Invalid('Sandbox output exceeds limits')
            for rel in payload.keys() - returned.keys():
                safe_path(root, rel).unlink()
            for path, content, mode in decoded:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                path.chmod(0o755 if mode & 0o111 else 0o644)
            return result
        except subprocess.TimeoutExpired as error:
            raise Invalid('Isolated command timed out; its result was not applied') from error
        finally:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=15)
