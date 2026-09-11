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
