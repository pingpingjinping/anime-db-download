"""Unpack only incoming Anissia ZIPs; compare older ZIP members read-only."""
import re
import stat
import tempfile
import zipfile
from pathlib import Path

from subtitle_work_storage import _anissia_work_root, preserve_download, safe_name, work_hash_index

MAX_MEMBERS = 5000
MAX_OUTPUT_FILES = 500
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_DEPTH = 3


def member_path(info):
    name = info.filename
    if not info.flag_bits & 0x800:
        try:
            name = name.encode('cp437').decode('cp949')
        except (UnicodeError, LookupError):
            pass
    name = name.replace('\\', '/')
    if name.startswith('/') or re.match(r'^[A-Za-z]:', name):
        raise ValueError('Absolute ZIP member path')
    parts = name.split('/')
    if '..' in parts or '\0' in name:
        raise ValueError('Invalid ZIP member path')
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
        raise ValueError('ZIP member is not a regular file')
    parts = [safe_name(part, 180) for part in parts if part not in ('', '.')]
    if not parts:
        raise ValueError('Empty ZIP member path')
    return Path(*parts)


def stage_archive(archive_path, staging, budget, prefix=Path(), depth=0):
    if depth > MAX_DEPTH:
        raise ValueError('Nested ZIP depth limit exceeded')
    staged = []
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        budget['members'] += len(infos)
        if budget['members'] > MAX_MEMBERS:
            raise ValueError('ZIP member limit exceeded')
        for info in infos:
            relative = prefix / member_path(info)
            if info.is_dir() or info.filename.endswith('\\') or stat.S_ISDIR(info.external_attr >> 16):
                continue
            if info.flag_bits & 1:
                raise ValueError('Encrypted ZIP cannot be unpacked automatically')
            if info.file_size > MAX_EXPANDED_BYTES - budget['bytes']:
                raise ValueError('ZIP expanded size limit exceeded')
            # Unique staging names also preserve duplicate original ZIP paths.
            temporary = staging / str(budget['next'])
            budget['next'] += 1
            with archive.open(info) as source, temporary.open('wb') as output:
                for chunk in iter(lambda: source.read(256 * 1024), b''):
                    budget['bytes'] += len(chunk)
                    if budget['bytes'] > MAX_EXPANDED_BYTES:
                        raise ValueError('ZIP expanded size limit exceeded')
                    output.write(chunk)
            # Reading every entry to EOF validates CRC before any final write.
            if relative.suffix.lower() == '.zip':
                staged.extend(stage_archive(temporary, staging, budget, relative.with_suffix(''), depth + 1))
            else:
                staged.append((temporary, relative))
            if len(staged) > MAX_OUTPUT_FILES:
                raise ValueError('ZIP output file limit exceeded')
    if not staged:
        raise ValueError('ZIP contains no usable files')
    return staged


class ExistingZipIndex:
    """A duplicate may keep referring to an older ZIP without moving that ZIP."""
    def __init__(self, root, excluded):
        self.root = root
        self.excluded = excluded

    def match(self, path):
        with work_hash_index(self.root) as index:
            return index.match_zip_member(path, self.excluded)


def _keep_file(temporary, destination, index, writes):
    # Use its final suffix to identify subtitle data while staging names stay unique.
    typed_temporary = temporary.with_suffix(destination.suffix)
    temporary.rename(typed_temporary)
    archive = index.match(typed_temporary)
    if archive is not None:
        typed_temporary.unlink()
        writes.reused.add(archive)
        print(f'[=] SHA-256 duplicate ZIP member: {destination} -> reuse {archive}')
        return archive
    destination.parent.mkdir(parents=True, exist_ok=True)
    return preserve_download(typed_temporary, destination)


def prepare_anissia_files(files, writes):
    """Return canonical files, retaining a new ZIP if complete extraction fails.

    Existing files and archives are read-only. Only files in this download's
    `created` set may be unpacked/removed. All regular assets, including fonts,
    keep the ZIP's folder layout under a folder named after the archive.
    """
    excluded = {path for path in writes.created if path.suffix.lower() == '.zip'}
    indexes = {}
    results = []
    for path in dict.fromkeys(files):
        root = _anissia_work_root(path)
        if root is None or path not in writes.created:
            results.append(path)
            continue
        if root not in indexes:
            indexes[root] = ExistingZipIndex(root, excluded)
        index = indexes[root]
        if path.suffix.lower() != '.zip':
            archive = index.match(path)
            if archive is not None:
                path.unlink()
                writes.created.discard(path)
                writes.reused.add(archive)
                print(f'[=] SHA-256 duplicate ZIP member: {path} -> reuse {archive}')
                path = archive
            results.append(path)
            continue

        before_created, before_reused = set(writes.created), set(writes.reused)
        try:
            with tempfile.TemporaryDirectory(prefix='.anissia-unzip-') as tmp:
                staging = Path(tmp)
                staged = stage_archive(path, staging, {'members': 0, 'bytes': 0, 'next': 0})
                destination = path.parent / safe_name(path.stem, 180)
                outputs = []
                for temporary, relative in staged:
                    final = destination / relative
                    _anissia_work_root(final)  # Reject linked destination directories.
                    outputs.append(_keep_file(temporary, final, index, writes))
            path.unlink()  # Every member has a verified, retained representation.
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
            # Roll back only outputs created by this extraction; keep the raw ZIP.
            for output in writes.created - before_created:
                output.unlink(missing_ok=True)
            writes.created.intersection_update(before_created)
            writes.reused.intersection_update(before_reused)
            results.append(path)
            print(f'[~] Anissia ZIP kept without unpacking: {path.name}: {exc}')
        else:
            writes.created.discard(path)
            results.extend(outputs)
            print(f'[+] Anissia ZIP unpacked: {path.name} -> {len(set(outputs))} kept file(s)')
    return list(dict.fromkeys(results))
