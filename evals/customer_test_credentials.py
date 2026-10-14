"""User-operated, private sandbox credential storage for authorized QA runs.

This does not collect credentials through chat. The user runs --install and
enters/submits the value themselves. No credential value is ever printed.
"""
from __future__ import annotations
import argparse
import getpass
import os
from pathlib import Path
import stat
import sys
import warnings


class CredentialStorageError(ValueError):
    pass


def _parent(path, create=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or len(path.parts) < 3:
        raise CredentialStorageError('An absolute private credential path is required.')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:-1]):
            final = index == len(path.parts[1:-1]) - 1
            if final and create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
            info = os.fstat(fd)
            if final and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
                raise CredentialStorageError('The credential directory must be owned by this user with mode 700.')
        return fd, path.name
    except Exception:
        os.close(fd)
        raise


def _validate(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or not value.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value):
        raise CredentialStorageError('The entered credential has an invalid format; nothing was saved.')
    return value


def save_key(path, value):
    value = _validate(value)
    parent, name = _parent(path, create=True)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


def read_saved_key(path):
    parent, name = _parent(path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, 'r') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or not 1 <= info.st_size <= 4096):
                raise CredentialStorageError('Credential ownership, permissions or file type is unsafe.')
            return _validate(stream.read(4097))
    finally:
        os.close(parent)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        parent, name = _parent(args.install, create=True)
        try:
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise CredentialStorageError('A credential path already exists; it will not be overwritten.')
        finally:
            os.close(parent)
        print('Enter saves the key to this private sandbox file:', args.install)
        print('It persists across stops/restarts and may be included in Daytona disk snapshots. It is not encrypted by this script.')
        if not sys.stdin.isatty():
            raise CredentialStorageError('A private interactive terminal is required.')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            value = getpass.getpass('Anthropic API key (hidden; Enter saves): ')
        save_key(args.install, value)
        value = ''
        print('Credential saved with owner-only access. The value was not printed.')
        return 0
    except (OSError, ValueError, getpass.GetPassWarning):
        print('Credential setup stopped: existing file, unsafe permissions/path, or invalid input. No credential value was printed.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
