"""Local activity watcher. Standard library only; never logs session contents."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import time

BASE = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))
TAIL_BYTES = 8192
MAX_READ = 1 << 20  # Per-poll ceiling; a burst larger than this drops its oldest part.
ANCHOR = 32         # Bytes re-read to confirm the file was appended to, not replaced.
MAX_SESSIONS = 40
DEFAULT_CLAUDE_DIR = Path.home() / '.claude/projects'
STRING = re.compile(rb'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"')
NUMBER = re.compile(rb'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')


class Projection:
    """Validate JSON structure while decoding only allowlisted metadata.

    Unselected values are skipped as opaque bytes, never json.loads'ed,
    decoded to text, returned, or logged. No conversation object is built.
    type is a structural discriminator, never exported.
    """
    def __init__(self, data):
        self.data, self.i = data, 0

    def ws(self):
        while self.i < len(self.data) and self.data[self.i] in b' \r\n\t':
            self.i += 1

    def string(self, keep=False):
        match = STRING.match(self.data, self.i)
        if not match:
            raise ValueError('Invalid JSON string')
        start, self.i = self.i, match.end()
        if keep and self.i - start <= 512:
            return json.loads(self.data[start:self.i])
        return None

    def value(self, schema=None, depth=0):
        if depth > 64:
            raise ValueError('Nesting limit')
        self.ws()
        c = self.data[self.i:self.i + 1]
        if c == b'"':
            return self.string(schema is True)
        if c in (b'{', b'['):
            obj = c == b'{'
            end = b'}' if obj else b']'
            result = {} if obj else []
            self.i += 1
            self.ws()
            if self.data[self.i:self.i + 1] == end:
                self.i += 1
                return result if schema is not None else None
            while True:
                child = None
                if obj:
                    self.ws()
                    key = self.string(isinstance(schema, dict))
                    self.ws()
                    if self.data[self.i:self.i + 1] != b':':
                        raise ValueError('Missing colon')
                    self.i += 1
                    if isinstance(schema, dict):
                        child = schema.get(key)
                elif isinstance(schema, list):
                    child = schema[0]
                val = self.value(child, depth + 1)
                if child is not None:
                    if obj:
                        if key in result:
                            raise ValueError('Duplicate metadata')
                        result[key] = val
                    else:
                        result.append(val)
                self.ws()
                sep = self.data[self.i:self.i + 1]
                self.i += 1
                if sep == end:
                    return result if schema is not None else None
                if sep != b',':
                    raise ValueError('Missing separator')
        for literal in (b'true', b'false', b'null'):
            if self.data.startswith(literal, self.i):
                self.i += len(literal)
                return None
        match = NUMBER.match(self.data, self.i)
        if not match:
            raise ValueError('Invalid JSON value')
        self.i = match.end()
        return None


SCHEMA = {'type': True, 'timestamp': True, 'message': {
    'content': [{'type': True, 'name': True, 'input': {'skill': True}}]}}


def metadata(line):
    parser = Projection(line)
    record = parser.value(SCHEMA)
    parser.ws()
    if parser.i != len(line) or not isinstance(record, dict):
        return []
    if record.get('type') != 'assistant':
        return []
    stamp = parse_time(record.get('timestamp'))
    message = record.get('message')
    if stamp is None or not isinstance(message, dict):
        return []
    content = message.get('content')
    if not isinstance(content, list):
        return []
    events = []
    for item in content:
        if not isinstance(item, dict) or item.get('type') != 'tool_use':
            continue
        name = item.get('name')
        if not isinstance(name, str) or not name or len(name) > 160:
            continue
        args = item.get('input')
        skill = args.get('skill') if isinstance(args, dict) else None
        events.append((stamp, name, skill if isinstance(skill, str) else None))
    return events


def parse_time(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def iso(stamp):
    return datetime.fromtimestamp(stamp, JST).isoformat(timespec='milliseconds')


def state_at(stamp, now):
    if stamp is None or now - stamp >= 300:
        return 'away'
    return 'working' if now - stamp <= 60 else 'idle'


def load_roster(path):
    # One JSON activity declaration beside each id in the JS ROSTER.
    source = path.read_text(encoding='utf-8').split('const ROSTER = [', 1)[1].split('\n];', 1)[0]
    entries = re.findall(r'id:"([a-z0-9_-]+)"[^\n]*\n\s*activity:(\{[^\n]+\}),', source)
    roster = {key: json.loads(activity) for key, activity in entries}
    if not roster or len(roster) != len(re.findall(r'\bid:"', source)):
        raise ValueError('Every ROSTER entry needs a JSON activity declaration')
    return roster


def safe_path(path):
    name = path.name.lower()
    return not any(word in name for word in ('api_key', 'secret', 'token', 'password'))


class Watcher:
    def __init__(self, roster, claude_dir, codex_dir, image_dirs, max_sessions=MAX_SESSIONS):
        if not isinstance(max_sessions, int) or max_sessions < 1:
            raise ValueError('max_sessions must be a positive integer')
        self.roster = roster
        self.claude_dir, self.codex_dir = claude_dir, codex_dir
        self.image_dirs = image_dirs
        self.seen = {key: (None, None) for key in roster}
        self.files = {}
        self.offsets = {}
        self.images = set()
        self.image_baseline = set()
        self.health = {}
        self.tasks = {}
        self.artifacts = []
        self.artifact_files = {}
        self.artifact_baseline = set()
        self.max_sessions = max_sessions
        self.claude_scan = {'found': 0, 'selected': 0, 'deferred': 0, 'limit': max_sessions}

    def claude_candidates(self):
        """Direct sessions plus one project-directory level; never follow links."""
        candidates, failures = [], 0

        def inspect(folder, include_projects):
            nonlocal failures
            try:
                for path in folder.iterdir():
                    if (not safe_path(path) or path.name.startswith('.') or path.is_symlink()
                            or getattr(path, 'is_junction', lambda: False)()):
                        continue
                    try:
                        if include_projects and path.is_dir():
                            inspect(path, False)
                        elif path.suffix == '.jsonl' and path.is_file():
                            candidates.append((path.stat().st_mtime_ns, path))
                    except OSError:
                        failures += 1
            except FileNotFoundError:
                pass
            except OSError:
                failures += 1

        inspect(self.claude_dir, True)
        return sorted(candidates, reverse=True), failures

    def mark(self, source, stamp, detail, now, skill=None, tool=None):
        """Attribute one event: skill name first, then tool name, then source."""
        if stamp > now + 5:
            return
        matches = [key for key, config in self.roster.items()
                   if skill is not None and skill in config.get('skills', [])]
        if not matches and tool is not None:
            matches = [key for key, config in self.roster.items()
                       if tool in config.get('tools', [])]
        if not matches:
            matches = [key for key, config in self.roster.items() if config.get('source') == source]
        for key in matches:
            previous = self.seen[key][0]
            if previous is None or stamp > previous:
                self.seen[key] = (stamp, detail)

    def scan_claude(self, now):
        candidates, failures = self.claude_candidates()
        selected = candidates[:self.max_sessions]
        deferred = len(candidates) - len(selected)
        self.claude_scan = {'found': len(candidates), 'selected': len(selected),
                            'deferred': deferred, 'limit': self.max_sessions}
        try:
            for _, path in selected:
                try:
                    stat = path.stat()
                    signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
                    previous = self.files.get(path)
                    if previous is not None and previous[0] == signature:
                        failures += previous[1]
                        continue
                    file_failures = 0
                    with path.open('rb') as stream:
                        # Resume where the last poll stopped so nothing is skipped
                        # between polls. The anchor proves the file was appended to
                        # rather than replaced; without it, restart from the tail.
                        offset, fresh = 0, True
                        state = self.offsets.get(path)
                        if state is not None and state[0] <= stat.st_size:
                            offset, anchor = state
                            stream.seek(offset - len(anchor))
                            fresh = stream.read(len(anchor)) != anchor
                        if fresh:
                            offset = max(0, stat.st_size - TAIL_BYTES)
                        elif stat.st_size - offset > MAX_READ:
                            offset, fresh = stat.st_size - MAX_READ, True
                        stream.seek(offset)
                        chunk = stream.read(stat.st_size - offset)
                    consumed = chunk.rfind(b'\n') + 1
                    lines = chunk[:consumed].split(b'\n')
                    if fresh and offset:
                        # The first fragment is the tail of a record we never saw whole.
                        lines = lines[1:]
                    if consumed:
                        self.offsets[path] = (offset + consumed,
                                              chunk[max(0, consumed - ANCHOR):consumed])
                    for line in lines:
                        if not line.strip():
                            continue
                        try:
                            for event_index, (stamp, name, skill) in enumerate(metadata(line)):
                                self.mark('claude', stamp, skill if name == 'Skill' and skill else name,
                                          now, skill if name == 'Skill' else None, name)
                                if name == 'Skill' and skill and stamp <= now + 5:
                                    self.tasks[(str(path), stamp, skill, event_index)] = {
                                        'skill': skill, 'timestamp': iso(stamp)}
                                    self.tasks = dict(sorted(self.tasks.items(),
                                        key=lambda entry: entry[0][1], reverse=True)[:30])
                        except (ValueError, TypeError, RecursionError, IndexError):
                            file_failures += 1
                    failures += file_failures
                    self.files[path] = (signature, file_failures)
                except OSError:
                    failures += 1
            self.health['claude'] = ('error' if failures else 'limited' if deferred
                                     else 'ok' if candidates else 'missing')
        except OSError:
            self.health['claude'] = 'error'

    def scan_codex(self, now):
        found, failed = False, False
        try:
            paths = set(self.codex_dir.glob('*.sqlite')) | set(self.codex_dir.glob('*-wal'))
            for path in paths:
                if not safe_path(path):
                    continue
                try:
                    stamp = path.stat().st_mtime
                    found = True
                    self.mark('codex', stamp, 'SQLite/WAL', now)
                except OSError:
                    failed = True
            self.health['codex'] = 'error' if failed else ('ok' if found else 'missing')
        except OSError:
            self.health['codex'] = 'error'

    def scan_images(self, now):
        failed, found = False, False
        for folder in self.image_dirs:
            try:
                current = {path for path in folder.iterdir() if safe_path(path)
                           and path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.gif')
                           and path.is_file()}
                found = True
                if folder in self.image_baseline and current - self.images:
                    self.mark('images', now, '新しい画像', now)
                self.images.difference_update(path for path in list(self.images) if path.parent == folder)
                self.images.update(current)
                self.image_baseline.add(folder)
            except OSError:
                failed = True
        self.health['images'] = 'error' if failed else ('ok' if found else 'missing')

    def poll(self, now=None):
        now = time.time() if now is None else now
        self.scan_claude(now)
        self.scan_codex(now)
        self.scan_images(now)
        self.scan_artifacts(now)
        return {'updatedAt': iso(now), 'agents': {
            key: {'state': state_at(stamp, now), 'lastSeen': iso(stamp) if stamp is not None else None,
                  'detail': detail}
            for key, (stamp, detail) in self.seen.items()}, 'sources': self.health.copy(),
            'tasks': list(self.tasks.values()), 'artifacts': self.artifacts.copy(),
            'claudeScan': self.claude_scan.copy()}

    def scan_artifacts(self, now):
        """Only file names and stat metadata, in explicitly monitored directories."""
        extensions = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.md', '.pdf',
                      '.docx', '.pptx', '.xlsx', '.html', '.css', '.js', '.py', '.csv'}
        failed = False
        for folder in self.image_dirs:
            try:
                current = {}
                for path in folder.iterdir():
                    if (path.name.startswith(('.', '~', '_')) or not safe_path(path)
                            or path.suffix.lower() not in extensions or not path.is_file()):
                        continue
                    stat = path.stat()
                    current[path] = (stat.st_mtime_ns, stat.st_size)
                if folder in self.artifact_baseline:
                    for path, signature in current.items():
                        previous = self.artifact_files.get(path)
                        if signature != previous:
                            self.artifacts.insert(0, {'filename': path.name, 'timestamp': iso(now),
                                                      'kind': 'created' if previous is None else 'updated'})
                    self.artifacts = self.artifacts[:30]
                self.artifact_files = {path: value for path, value in self.artifact_files.items()
                                       if path.parent != folder}
                self.artifact_files.update(current)
                self.artifact_baseline.add(folder)
            except OSError:
                failed = True
        self.health['artifacts'] = 'error' if failed else 'ok'


def write_status(path, payload):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--claude-dir', type=Path, default=DEFAULT_CLAUDE_DIR,
                        help='Claude projects root or a single project directory')
    parser.add_argument('--max-sessions', type=int, default=MAX_SESSIONS,
                        help='Maximum sessions selected by newest modification time (default: 40)')
    parser.add_argument('--codex-dir', type=Path, default=Path.home() / '.codex')
    parser.add_argument('--image-dir', action='append', type=Path, default=[], help='Additional image output directory (repeatable; non-recursive)')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if args.max_sessions < 1:
        parser.error('--max-sessions must be positive')
    watcher = Watcher(load_roster(BASE / 'preview.html'), args.claude_dir, args.codex_dir,
                      list(dict.fromkeys([BASE] + [path.resolve() for path in args.image_dir])),
                      max_sessions=args.max_sessions)
    print('Activity watcher: 4-second interval. Session content is never logged. Ctrl+C to stop.')
    try:
        while True:
            try:
                write_status(BASE / 'status.json', watcher.poll())
            except OSError:
                print('Could not update status.json; retrying next poll.')
            if args.once:
                break
            time.sleep(4)
    except KeyboardInterrupt:
        print('Watcher stopped.')


if __name__ == '__main__':
    main()
