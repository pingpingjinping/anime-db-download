"""Title folders with exact, per-file source identities; never rename originals."""
import fcntl
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path

MANIFEST = '.weekly-subtitle-sources.json'
LOCAL_INDEX_CHANGES = '.local-subtitle-search-updates.jsonl'


def safe_name(value: str, limit: int = 160) -> str:
    value = unicodedata.normalize('NFKC', value or '').strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value)
    value = re.sub(r'\s+', ' ', value).strip(' .')
    return value[:limit].rstrip(' .') or '_'


def source_identity(item):
    source = item.get('source', 'anissia')
    identity = str(item.get('post_id') if source == 'aniall' else item.get('anime_no') or '')
    if source not in ('aniall', 'anissia') or not re.fullmatch(r'[1-9]\d*', identity):
        raise ValueError('Missing subtitle source identity')
    if source == 'aniall' and item.get('source_url') != f'https://aniall.tistory.com/{identity}':
        raise ValueError('AniAll post ID and source URL disagree')
    return source, identity


def load_manifest(target):
    path = target / MANIFEST
    if not path.exists():
        return {'version': 1, 'files': {}}
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('version') != 1 or not isinstance(value.get('files'), dict):
        raise ValueError('Invalid subtitle source manifest')
    return value


def work_directory(item, target):
    source, identity = source_identity(item)
    title = str(item.get('title') if source == 'aniall' else item.get('anime_title') or '').strip()
    if not title:
        raise ValueError('Missing work title')
    corpus = target / f'{source}_subtitles'
    corpus.mkdir(parents=True, exist_ok=True)
    # A known source continues in its recorded work folder even after a title edit.
    recorded = set()
    for relative, record in load_manifest(target)['files'].items():
        parts = Path(relative).parts
        if len(parts) >= 3 and parts[0] == corpus.name and all(p not in ('.', '..') for p in parts):
            if any(s.get('source') == source and str(s.get('id')) == identity for s in record.get('sources', [])):
                recorded.add(parts[1])
    key = lambda v: unicodedata.normalize('NFKC', v).casefold().strip()
    folders = [p for p in corpus.iterdir() if p.is_dir() and not p.is_symlink()]
    matching = [p for p in folders if key(p.name) == key(safe_name(title))]
    previous = [p for p in folders if p.name in recorded]
    numbered = [p for p in folders if p.name.startswith(identity + '__')]
    candidates = previous or matching or numbered
    if len(candidates) > 1:
        raise ValueError('Multiple existing folders for this source; manual consolidation required')
    directory = candidates[0] if candidates else corpus / safe_name(title)
    if directory.is_symlink() or corpus.is_symlink():
        raise ValueError('Subtitle work directory cannot be a symlink')
    return directory


def register_sources(item, files, target):
    with (target / ".weekly-subtitle-sources.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _register_sources(item, files, target)


def _register_sources(item, files, target):
    source, identity = source_identity(item)
    manifest = load_manifest(target)
    for file in files:
        relative = file.relative_to(target).as_posix()
        if not relative.startswith(source + '_subtitles/') or '..' in Path(relative).parts:
            raise ValueError('Invalid downloaded source path')
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        record = manifest['files'].get(relative, {})
        if record and record.get('sha256') != digest:
            raise ValueError('An existing registered subtitle was changed')
        sources = record.get('sources', [])
        row = {'source': source, 'id': identity, 'title': item.get('anime_title') or item.get('title') or '',
               'episode': str(item.get('episode') or ''), 'creator': str(item.get('creator') or ''),
               'source_url': str(item.get('source_url') or '')}
        if row not in sources:
            sources.append(row)
        manifest['files'][relative] = {'sha256': digest, 'sources': sources}
    temporary = target / f'{MANIFEST}.{os.getpid()}.tmp'
    temporary.write_text(json.dumps(manifest, ensure_ascii=False) + '\n', encoding='utf-8')
    temporary.replace(target / MANIFEST)
    # The web catalog indexes physical ZIP names. Notify it only after the
    # source manifest commits, under this same flock. The Pi app replays only
    # newly appended paths; downloading one subtitle never rescans 50k files.
    # Indexing is an acceleration, not part of the subtitle write transaction.
    try:
        with (target / LOCAL_INDEX_CHANGES).open('a', encoding='utf-8') as updates:
            for file in files:
                updates.write(json.dumps({'path': file.relative_to(target).as_posix()}, ensure_ascii=False) + '\\n')
    except OSError as exc:
        print(f'Warning: local subtitle search index notification failed: {exc}')


def preserve_download(temporary, destination):
    """Reuse identical bytes; keep different same-name variants without overwriting."""
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
            temporary.unlink()
            return destination
        destination = destination.with_name(f'{destination.stem} [{digest[:16]}]{destination.suffix}')
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ValueError('Subtitle filename digest collision')
            temporary.unlink()
            return destination
    temporary.replace(destination)
    return destination
