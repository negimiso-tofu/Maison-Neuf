"""Synthetic data only. Run: python -m unittest -v test_watcher"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import watcher


def event(stamp, name='Read', skill=None, **extra):
    return json.dumps({'type': 'assistant', 'timestamp': watcher.iso(stamp),
                       'message': {'content': [
                           {'type': 'text', 'text': 'PRIVATE_CONVERSATION'},
                           {'type': 'thinking', 'thinking': 'PRIVATE_THINKING'},
                           {'type': 'tool_use', 'name': name,
                            'input': {'skill': skill, 'command': 'PRIVATE_COMMAND', **extra}}]}},
                      ensure_ascii=False).encode('utf-8') + b'\n'


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.claude = self.root / 'claude'
        self.codex = self.root / 'codex'
        self.images = self.root / 'images'
        for folder in (self.claude, self.codex, self.images):
            folder.mkdir()
        self.roster = watcher.load_roster(watcher.BASE / 'preview.html')
        self.w = watcher.Watcher(self.roster, self.claude, self.codex, [self.images])
        self.now = 1800000000.0

    def test_only_metadata_strings_are_decoded(self):
        original = json.loads
        decoded = []

        def guarded(value, *args, **kwargs):
            self.assertNotIn(b'PRIVATE_', value)
            decoded.append(value)
            return original(value, *args, **kwargs)

        with patch.object(watcher.json, 'loads', guarded):
            result = watcher.metadata(event(self.now, 'Skill', 'company-check'))
        self.assertEqual(result, [(self.now, 'Skill', 'company-check')])
        self.assertTrue(decoded)

    def test_fake_tool_in_text_and_other_paths_is_ignored(self):
        record = {'type': 'assistant', 'timestamp': watcher.iso(self.now),
                  'message': {'content': [{'type': 'text', 'text':
                      '{"type":"tool_use","name":"Skill","input":{"skill":"company-check"}}'}]},
                  'unrelated': {'type': 'tool_use', 'name': 'Read'}}
        self.assertEqual(watcher.metadata(json.dumps(record).encode()), [])

    def test_all_sessions_and_skill_mappings(self):
        for index, (key, config) in enumerate(self.roster.items()):
            for offset, skill in enumerate(config['skills']):
                (self.claude / f'{index}-{offset}.jsonl').write_bytes(event(self.now, 'Skill', skill))
        (self.claude / 'generic.jsonl').write_bytes(event(self.now, 'Read'))
        status = self.w.poll(self.now)
        for key, config in self.roster.items():
            if config['source'] in ('claude', 'skill'):
                self.assertEqual(status['agents'][key]['state'], 'working', key)
        self.assertNotIn('PRIVATE_', json.dumps(status))

    def test_boundaries_and_no_new_event_on_repeated_poll(self):
        (self.claude / 'one.jsonl').write_bytes(event(self.now))
        for elapsed, state in ((0, 'working'), (60, 'working'), (60.001, 'idle'),
                               (299.999, 'idle'), (300, 'away')):
            self.assertEqual(self.w.poll(self.now + elapsed)['agents']['clarice']['state'], state)

    def test_tail_seek_partial_append_and_bad_record(self):
        path = self.claude / 'large.jsonl'
        pending = event(self.now, 'Skill', 'タスク整理')
        path.write_bytes(b'{"text":"' + b'x' * (14 * 1024 * 1024) + b'"}\n' +
                         b'not-json\n' + event(self.now - 10) + pending[:40])
        original = Path.open
        reads = []

        class BoundedReader:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def seek(self, position):
                self.position = position
                return self.stream.seek(position)
            def read(self, count):
                reads.append((self.position, count))
                return self.stream.read(count)

        def bounded(file, *args, **kwargs):
            stream = original(file, *args, **kwargs)
            return BoundedReader(stream) if file == path and args == ('rb',) else stream

        with patch.object(Path, 'open', bounded):
            status = self.w.poll(self.now)
        self.assertTrue(all(start > 0 and count == 8192 for start, count in reads))
        self.assertEqual(status['agents']['clarice']['state'], 'working')
        self.assertEqual(status['agents']['iris']['state'], 'away')
        with path.open('ab') as stream:
            stream.write(pending[40:])
        self.assertEqual(self.w.poll(self.now)['agents']['iris']['state'], 'working')

    def test_rotation_and_unknown_skill(self):
        path = self.claude / 'one.jsonl'
        path.write_bytes(event(self.now - 100))
        self.w.poll(self.now)
        path.write_bytes(event(self.now, 'Skill', 'unknown-skill'))
        result = self.w.poll(self.now)['agents']['clarice']
        self.assertEqual(result['state'], 'working')
        self.assertEqual(result['detail'], 'unknown-skill')

    def test_sqlite_and_wal_stat_only(self):
        path = self.codex / 'state.sqlite-wal'
        path.write_bytes(b'PRIVATE_DATABASE')
        os.utime(path, (self.now, self.now))
        with patch.object(Path, 'open', side_effect=AssertionError('No database read')):
            self.w.scan_codex(self.now)
        self.assertEqual(self.w.seen['colette'][0], self.now)

    def test_images_baseline_new_file_and_no_retrigger(self):
        (self.images / 'existing.png').write_bytes(b'fake')
        self.assertEqual(self.w.poll(self.now)['agents']['lumiere']['state'], 'away')
        (self.images / 'new.png').write_bytes(b'fake')
        self.assertEqual(self.w.poll(self.now + 4)['agents']['lumiere']['state'], 'working')
        self.assertEqual(self.w.poll(self.now + 304)['agents']['lumiere']['state'], 'away')

    def test_future_timestamp_and_invalid_metadata(self):
        (self.claude / 'future.jsonl').write_bytes(event(self.now + 1000))
        self.assertEqual(self.w.poll(self.now)['agents']['clarice']['state'], 'away')
        for data in (b'null', b'[]', b'{"type":"assistant","timestamp":{}}',
                     b'{"type":"assistant","timestamp":123}'):
            self.assertEqual(watcher.metadata(data), [])

    def test_task_history_is_deduplicated_and_bounded(self):
        path = self.claude / 'skills.jsonl'
        for index in range(35):
            with path.open('ab') as stream:
                stream.write(event(self.now + index, 'Skill', 'company-check'))
            result = self.w.poll(self.now + index)
        self.assertEqual(len(result['tasks']), 30)
        self.assertEqual(result['tasks'][0]['timestamp'], watcher.iso(self.now + 34))
        self.assertEqual(self.w.poll(self.now + 36)['tasks'], result['tasks'])
        self.assertEqual(set(result['tasks'][0]), {'skill', 'timestamp'})

    def test_artifacts_baseline_update_and_private_names(self):
        path = self.images / 'report.md'
        path.write_text('PRIVATE_BODY', encoding='utf-8')
        self.assertEqual(self.w.poll(self.now)['artifacts'], [])
        path.write_text('PRIVATE_BODY_UPDATED', encoding='utf-8')
        (self.images / 'new.pdf').write_bytes(b'PRIVATE_CONTENT')
        self.assertFalse(watcher.safe_path(Path('SECRET_report.md')))
        result = self.w.poll(self.now + 4)
        self.assertEqual({item['filename'] for item in result['artifacts']}, {'report.md', 'new.pdf'})
        self.assertNotIn('PRIVATE_', json.dumps(result))
        self.assertEqual(self.w.poll(self.now + 8)['artifacts'], result['artifacts'])

    def test_tool_assignments(self):
        assignments = {'Write': 'celine', 'Grep': 'sylvia', 'WebSearch': 'verity',
                       'AskUserQuestion': 'rosalie', 'TodoWrite': 'iris',
                       'Agent': 'aurelia', 'Artifact': 'lumiere'}
        for index, (tool, expected) in enumerate(assignments.items()):
            with self.subTest(tool=tool):
                fresh = watcher.Watcher(self.roster, self.claude, self.codex, [self.images])
                fresh.mark('claude', self.now, tool, self.now, tool=tool)
                self.assertEqual([key for key, value in fresh.seen.items() if value[0] is not None], [expected])
                (self.claude / f'tool-{index}.jsonl').write_bytes(event(self.now, tool))
        result = self.w.poll(self.now)
        for expected in assignments.values():
            self.assertEqual(result['agents'][expected]['state'], 'working')
        self.assertEqual(result['agents']['clarice']['state'], 'away')

    def test_skill_then_tool_then_source_priority(self):
        self.w.mark('claude', self.now, 'company-check', self.now,
                    skill='company-check', tool='Write')
        self.assertEqual(self.w.seen['verity'][0], self.now)
        self.assertIsNone(self.w.seen['celine'][0])
        self.w.mark('claude', self.now + 1, 'Write', self.now + 1,
                    skill='unknown-skill', tool='Write')
        self.assertEqual(self.w.seen['celine'][0], self.now + 1)
        self.w.mark('claude', self.now + 2, 'unknown-tool', self.now + 2,
                    skill='unknown-skill', tool='unknown-tool')
        self.assertEqual(self.w.seen['clarice'][0], self.now + 2)

    def test_resume_keeps_events_before_large_append_tail(self):
        path = self.claude / 'burst.jsonl'
        path.write_bytes(event(self.now - 100))
        self.w.poll(self.now)
        # The unique Skill event is outside the final 8KiB, but after the saved cursor.
        burst = event(self.now, 'Skill', 'company-check') + event(self.now, 'Read') * 60
        self.assertGreater(len(burst), watcher.TAIL_BYTES)
        self.assertLess(len(burst), watcher.MAX_READ)
        with path.open('ab') as stream:
            stream.write(burst)
        result = self.w.poll(self.now)
        self.assertEqual(result['agents']['verity']['state'], 'working')
        self.assertEqual(len(result['tasks']), 1)
        self.assertEqual(self.w.poll(self.now + 4)['tasks'], result['tasks'])

    def test_same_length_replacement_checks_anchor_and_restarts(self):
        path = self.claude / 'replaced.jsonl'
        before = event(self.now - 100, 'Read', padding='AAAA')
        after = event(self.now, 'Edit', padding='BBBB')
        self.assertEqual(len(before), len(after))
        path.write_bytes(before)
        self.w.poll(self.now)
        saved = path.stat()
        path.write_bytes(after)
        os.utime(path, ns=(saved.st_atime_ns, saved.st_mtime_ns + 1000000))
        result = self.w.poll(self.now)
        self.assertEqual(result['agents']['celine']['state'], 'working')
        self.assertEqual(result['agents']['celine']['detail'], 'Edit')
        self.assertEqual(self.w.offsets[path][0], len(after))

    def test_projects_root_and_direct_sessions_are_both_monitored(self):
        for name, tool in (('project-a', 'Write'), ('project-b', 'WebSearch')):
            folder = self.claude / name
            folder.mkdir()
            (folder / 'session.jsonl').write_bytes(event(self.now, tool))
        (self.claude / 'direct.jsonl').write_bytes(event(self.now, 'Read'))
        result = self.w.poll(self.now)
        for key in ('celine', 'verity', 'clarice'):
            self.assertEqual(result['agents'][key]['state'], 'working')
        self.assertEqual(result['claudeScan']['selected'], 3)
        self.assertEqual(result['sources']['claude'], 'ok')
        single = watcher.Watcher(self.roster, self.claude / 'project-b', self.codex, [self.images])
        self.assertEqual(single.poll(self.now)['agents']['verity']['state'], 'working')

    def test_newest_session_limit_and_updated_project_promotion(self):
        paths = []
        for index, tool in enumerate(('WebSearch', 'Write', 'Grep')):
            folder = self.claude / f'project-{index}'
            folder.mkdir()
            path = folder / 'session.jsonl'
            path.write_bytes(event(self.now, tool))
            os.utime(path, (self.now + index, self.now + index))
            paths.append(path)
        limited = watcher.Watcher(self.roster, self.claude, self.codex, [self.images], max_sessions=2)
        with patch.object(watcher, 'metadata', wraps=watcher.metadata) as parse:
            result = limited.poll(self.now)
        self.assertEqual(parse.call_count, 2)
        self.assertEqual(result['agents']['verity']['state'], 'away')
        self.assertEqual(result['agents']['celine']['state'], 'working')
        self.assertEqual(result['agents']['sylvia']['state'], 'working')
        self.assertEqual(result['sources']['claude'], 'limited')
        self.assertEqual(result['claudeScan'], {'found': 3, 'selected': 2, 'deferred': 1, 'limit': 2})
        paths[0].write_bytes(event(self.now + 4, 'WebSearch'))
        os.utime(paths[0], (self.now + 4, self.now + 4))
        self.assertEqual(limited.poll(self.now + 4)['agents']['verity']['state'], 'working')
        self.assertEqual(limited.poll(self.now + 8)['sources']['claude'], 'limited')

    def test_project_scan_does_not_descend_beyond_one_level(self):
        project = self.claude / 'project-a'
        nested = project / 'nested'
        nested.mkdir(parents=True)
        (nested / 'ignored.jsonl').write_bytes(event(self.now, 'Write'))
        (project / 'session.jsonl').write_bytes(event(self.now, 'Read'))
        result = self.w.poll(self.now)
        self.assertEqual(result['claudeScan']['found'], 1)
        self.assertEqual(result['agents']['celine']['state'], 'away')
        for name in ('API_KEY.jsonl', 'SECRET.jsonl', 'TOKEN.jsonl', 'PASSWORD.jsonl'):
            self.assertFalse(watcher.safe_path(Path(name)))

    def test_unreadable_project_reports_error_without_hiding_other_projects(self):
        blocked = self.claude / 'blocked-project'
        blocked.mkdir()
        (self.claude / 'readable.jsonl').write_bytes(event(self.now))
        original = Path.iterdir
        def listing(path):
            if path == blocked:
                raise PermissionError('Synthetic test error')
            return original(path)
        with patch.object(Path, 'iterdir', listing):
            result = self.w.poll(self.now)
        self.assertEqual(result['sources']['claude'], 'error')
        self.assertEqual(result['agents']['clarice']['state'], 'working')

    def test_default_project_root_and_session_limit_validation(self):
        self.assertEqual(watcher.DEFAULT_CLAUDE_DIR, Path.home() / '.claude/projects')
        with self.assertRaises(ValueError):
            watcher.Watcher(self.roster, self.claude, self.codex, [self.images], max_sessions=0)
        (self.claude / 'one.jsonl').write_bytes(event(self.now))
        exact = watcher.Watcher(self.roster, self.claude, self.codex, [self.images], max_sessions=1)
        self.assertEqual(exact.poll(self.now)['sources']['claude'], 'ok')

    def test_status_file_and_missing_sources(self):
        self.w.claude_dir = self.root / 'missing'
        payload = self.w.poll(self.now)
        path = self.root / 'status.json'
        watcher.write_status(path, payload)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), payload)
        self.assertFalse(path.with_name('status.json.tmp').exists())
        self.assertEqual(payload['sources']['claude'], 'missing')


if __name__ == '__main__':
    unittest.main()
