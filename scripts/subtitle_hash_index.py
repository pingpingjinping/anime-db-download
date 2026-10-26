"""Persistent, stat-validated SHA-256 cache inside one Anissia work folder."""
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path

INDEX_NAME = '.subtitle-sha256-index.json'
LOCK_NAME = '.subtitle-sha256-index.lock'
SUBTITLES = {'.ass', '.ssa', '.smi', '.srt', '.vtt'}
MAX_MEMBERS = 5000
MAX_MEMBER_BYTES = 256 * 1024 * 1024
HEX = re.compile(r'^[a-f0-9]{64}$')


def sha256_file(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(256 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def signature(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Hash index only accepts regular files')
    return [info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev]


def usable_name(name):
    return (name not in (INDEX_NAME, LOCK_NAME)
            and not name.startswith(('.subtitle-sha256-index.', '.winpng-'))
            and not name.endswith(('.part', '.tmp')) and Path(name).suffix.lower() != '.jmk')


def load_index(root):
    try:
        path = root / INDEX_NAME
        if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('version') != 1 or not isinstance(value.get('files'), dict):
            return {}
        rows = {}
        for relative, row in value['files'].items():
            parts = Path(relative).parts
            if (not parts or Path(relative).is_absolute() or '..' in parts
                    or not usable_name(parts[-1]) or not isinstance(row, dict)):
                continue
            saved = row.get('signature')
            if not isinstance(saved, list) or len(saved) != 5 or not all(type(x) is int for x in saved):
                continue
            clean = {'signature': saved}
            if isinstance(row.get('sha256'), str) and HEX.fullmatch(row['sha256']):
                clean['sha256'] = row['sha256']
            members = row.get('zip_members')
            if isinstance(members, list) and len(members) <= MAX_MEMBERS and all(
                isinstance(m, dict) and isinstance(m.get('name'), str)
                and type(m.get('size')) is int and m['size'] >= 0
                and isinstance(m.get('sha256'), str) and HEX.fullmatch(m['sha256']) for m in members
            ):
                clean['zip_members'] = members
            rows[relative] = clean
        return rows
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


class WorkHashIndex:
    def __init__(self, root):
        self.root = root
        self.files = load_index(root)
        self.dirty = not (root / INDEX_NAME).exists()
        self.refresh()

    def refresh(self):
        current = {}
        for directory, folders, names in os.walk(self.root, followlinks=False):
            folders[:] = sorted(name for name in folders if not Path(directory, name).is_symlink())
            for name in sorted(names):
                if not usable_name(name):
                    continue
                path = Path(directory, name)
                try:
                    stamp = signature(path)
                except (OSError, ValueError):
                    continue
                relative = path.relative_to(self.root).as_posix()
                previous = self.files.get(relative, {})
                current[relative] = previous if previous.get('signature') == stamp else {'signature': stamp}
        if current != self.files:
            self.dirty = True
        self.files = current

    def remember(self, path, digest=None):
        relative = path.relative_to(self.root).as_posix()
        stamp = signature(path)
        row = self.files.get(relative, {})
        if row.get('signature') != stamp:
            row = {'signature': stamp}
        if digest is not None:
            row = {**row, 'sha256': digest}
        if row != self.files.get(relative):
            self.files[relative] = row
            self.dirty = True
        return row

    def digest(self, path):
        row = self.remember(path)
        if 'sha256' not in row:
            before = row['signature']
            digest = sha256_file(path)
            if signature(path) != before:
                raise RuntimeError('Subtitle changed while hashing')
            row = self.remember(path, digest)
        return row['sha256']

    def find_identical(self, incoming, digest):
        size = incoming.stat().st_size
        for relative, row in list(self.files.items()):
            candidate = self.root / relative
            if candidate == incoming or row['signature'][0] != size:
                continue
            try:
                if self.digest(candidate) == digest:
                    return candidate
            except (FileNotFoundError, ValueError):
                self.files.pop(relative, None)
                self.dirty = True
        return None

    def _zip_members(self, path):
        row = self.remember(path)
        if 'zip_members' in row:
            return row['zip_members']
        before = row['signature']
        members, total = [], 0
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_MEMBERS:
                    raise ValueError('ZIP member limit exceeded')
                for info in infos:
                    name = info.filename.replace('\\', '/')
                    if (info.is_dir() or info.flag_bits & 1 or name.startswith('/')
                            or re.match(r'^[A-Za-z]:', name) or '..' in name.split('/')
                            or stat.S_ISLNK(info.external_attr >> 16)
                            or Path(name).suffix.lower() not in SUBTITLES):
                        continue
                    total += info.file_size
                    if total > MAX_MEMBER_BYTES:
                        raise ValueError('ZIP subtitle byte limit exceeded')
                    value = hashlib.sha256()
                    with archive.open(info) as source:
                        for chunk in iter(lambda: source.read(256 * 1024), b''):
                            value.update(chunk)
                    members.append({'name': info.filename, 'size': info.file_size, 'sha256': value.hexdigest()})
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
            # Never use a partial/corrupt member index as evidence to remove data.
            members = []
        if signature(path) != before:
            raise RuntimeError('ZIP changed while hashing members')
        row['zip_members'] = members
        self.dirty = True
        return members

    def match_zip_member(self, incoming, excluded):
        if incoming.suffix.lower() not in SUBTITLES:
            return None
        size, digest = incoming.stat().st_size, None
        for relative in list(self.files):
            path = self.root / relative
            if path.suffix.lower() != '.zip' or path in excluded:
                continue
            try:
                for member in self._zip_members(path):
                    if member['size'] == size:
                        digest = digest or (self.digest(incoming) if incoming.is_relative_to(self.root)
                                            else sha256_file(incoming))
                        if member['sha256'] == digest:
                            return path
            except (OSError, ValueError, RuntimeError):
                continue
        return None

    def save(self):
        self.refresh()  # Also removes extracted ZIPs and rolled-back output paths.
        if not self.dirty:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / LOCK_NAME
        if lock_path.is_symlink():
            raise ValueError('Hash index lock cannot be a symlink')
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Keep hashes another writer has already computed for the same bytes.
            latest = load_index(self.root)
            for relative, row in self.files.items():
                other = latest.get(relative, {})
                if row['signature'] == other.get('signature'):
                    self.files[relative] = {**other, **row}
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.root,
                                                 prefix='.subtitle-sha256-index.', suffix='.tmp', delete=False) as out:
                    temporary = Path(out.name)
                    json.dump({'version': 1, 'files': self.files}, out, ensure_ascii=False)
                    out.write('\n')
                temporary.replace(self.root / INDEX_NAME)
                self.dirty = False
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
