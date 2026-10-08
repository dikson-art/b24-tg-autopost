"""Offline tests: local Git remotes and mocked Bitrix/site/Telegram only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

CODE = Path(__file__).resolve().parents[1] / 'tools' / 'b24-autopost'
sys.path.insert(0, str(CODE))
import service
from state_store import StateError, merge_state, persist


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class GitStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.remote = root / 'remote.git'
        self.repo = root / 'runner'
        git(root, 'init', '--bare', str(self.remote))
        git(root, 'init', '-b', 'main', str(self.repo))
        git(self.repo, 'config', 'user.name', 'test')
        git(self.repo, 'config', 'user.email', 'test@example.com')
        self.path = self.repo / 'tools/b24-autopost/state.json'
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"files": {}}')
        (self.repo / 'code.txt').write_text('original')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'initial')
        git(self.repo, 'remote', 'add', 'origin', str(self.remote))
        git(self.repo, 'push', 'origin', 'main')
        self.state = {'files': {'clip': {'status': 'pending', 'attempt_id': 'a'}}, 'updated': '1'}

    def read_remote(self):
        return json.loads(git(self.remote, 'show', 'main:tools/b24-autopost/state.json'))

    def test_reservation_then_confirmation(self):
        persist(self.path, self.state)
        self.assertEqual(self.read_remote()['files']['clip']['status'], 'pending')
        self.state['files']['clip'].update(status='published', message_id=123)
        persist(self.path, self.state)
        self.assertEqual(self.read_remote()['files']['clip']['message_id'], 123)
        # Running again after an uncertain push acknowledgement is idempotent.
        head = git(self.remote, 'rev-parse', 'main')
        persist(self.path, self.state)
        self.assertEqual(git(self.remote, 'rev-parse', 'main'), head)

    def test_push_race_preserves_remote_code_and_records(self):
        other = self.repo.parent / 'other'
        git(self.repo.parent, 'clone', '-b', 'main', str(self.remote), str(other))
        git(other, 'config', 'user.name', 'test')
        git(other, 'config', 'user.email', 'test@example.com')
        actual_run = subprocess.run
        raced = False
        def race(args, **kwargs):
            nonlocal raced
            if args[:2] == ['git', 'push'] and not raced:
                raced = True
                (other / 'code.txt').write_text('concurrent edit')
                (other / 'tools/b24-autopost/state.json').write_text(json.dumps({'files': {'other': {'status': 'published', 'message_id': 9}}}))
                git(other, 'add', '.')
                git(other, 'commit', '-m', 'concurrent')
                git(other, 'push', 'origin', 'main')
            return actual_run(args, **kwargs)
        with patch('subprocess.run', side_effect=race), patch('state_store.time.sleep'):
            persist(self.path, self.state)
        self.assertTrue(raced)
        self.assertEqual(set(self.read_remote()['files']), {'clip', 'other'})
        self.assertEqual(git(self.remote, 'show', 'main:code.txt'), 'concurrent edit')
        self.assertEqual((self.repo / 'code.txt').read_text(), 'original')

    def test_exhausted_confirmation_leaves_durable_pending(self):
        persist(self.path, self.state)
        hook = self.remote / 'hooks/pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        self.state['files']['clip'].update(status='published', message_id=123)
        with patch('state_store.time.sleep'), self.assertRaises(StateError):
            persist(self.path, self.state, attempts=2)
        self.assertEqual(self.read_remote()['files']['clip']['status'], 'pending')

    def test_lost_push_response_reconciles(self):
        actual_run = subprocess.run
        lost = False
        def lose_response(args, **kwargs):
            nonlocal lost
            result = actual_run(args, **kwargs)
            if args[:2] == ['git', 'push'] and not lost:
                lost = True
                raise subprocess.TimeoutExpired(args, 60)
            return result
        with patch('subprocess.run', side_effect=lose_response), patch('state_store.time.sleep'):
            persist(self.path, self.state)
        self.assertEqual(self.read_remote()['files']['clip']['status'], 'pending')

    def test_competing_reservation_blocks(self):
        persist(self.path, self.state)
        competing = {'files': {'clip': {'status': 'pending', 'attempt_id': 'b'}}}
        with self.assertRaises(StateError):
            persist(self.path, competing)
        self.assertEqual(self.read_remote()['files']['clip']['attempt_id'], 'a')


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state_path = self.root / 'state.json'
        self.state_path.write_text('{"files": {}}')
        self.cfg = {'bitrix': {'webhook_url': 'mock'}, 'telegram': {'bot_token': 'mock', 'chat_id': 'mock'}}
        def download(cfg, fid, path):
            Path(path).write_bytes(b'fake video')
        self.stack = []
        for target, kwargs in [
            ('BASE_DIR', {'new': str(self.root)}),
            ('log', {'new': lambda msg: None}),
            ('b24_find_folder', {'return_value': 1}),
            ('b24_subfolders', {'return_value': [{'ID': 2, 'NAME': 'lecture'}]}),
            ('b24_files', {'return_value': [{'ID': 3, 'NAME': 'clip(1).mp4', 'SIZE': 10}]}),
            ('site_find_lecture_page', {'return_value': 'https://example.com'}),
            ('site_fetch_lecture_info', {'return_value': {'title': 'test'}}),
            ('b24_download', {'side_effect': download}),
        ]:
            p = patch.object(service, target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'B24TG_DURABLE_STATE': '0', 'GITHUB_ACTIONS': 'false'})
        p.start()
        self.addCleanup(p.stop)
        # Any accidental network call fails the test immediately.
        p = patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden'))
        p.start()
        self.addCleanup(p.stop)

    def test_failed_reservation_never_sends(self):
        with patch.object(service, 'save_state', side_effect=StateError('push failed')), patch.object(service, 'tg_send_video') as send:
            with self.assertRaises(StateError):
                service.cmd_run(self.cfg, False, 1)
            send.assert_not_called()

    def test_failed_confirmation_does_not_resend_next_run(self):
        actual_save = service.save_state
        def checkpoint(path, state):
            if next(iter(state['files'].values()))['status'] == 'published':
                raise StateError('push failed')
            actual_save(path, state)
        with patch.object(service, 'save_state', side_effect=checkpoint), patch.object(service, 'tg_send_video', return_value={'message_id': 42}) as send:
            with self.assertRaises(StateError):
                service.cmd_run(self.cfg, False, 1)
            with self.assertRaises(StateError):
                service.cmd_run(self.cfg, False, 1)
            self.assertEqual(send.call_count, 1)

    def test_uncertain_telegram_response_blocks_retry(self):
        with patch.object(service, 'tg_send_video', side_effect=TimeoutError()) as send:
            self.assertEqual(service.cmd_run(self.cfg, False, 1), 1)
            with self.assertRaises(StateError):
                service.cmd_run(self.cfg, False, 1)
            self.assertEqual(send.call_count, 1)

    def test_success_and_dry_run(self):
        original = self.state_path.read_bytes()
        with patch.object(service, 'tg_send_video', return_value={'message_id': 42}) as send:
            self.assertEqual(service.cmd_run(self.cfg, True, 1), 0)
            send.assert_not_called()
            self.assertEqual(self.state_path.read_bytes(), original)
            self.assertEqual(service.cmd_run(self.cfg, False, 1), 0)
            self.assertEqual(service.cmd_run(self.cfg, False, 1), 0)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(next(iter(json.loads(self.state_path.read_text())['files'].values()))['message_id'], 42)

    def test_state_error_exit_classification(self):
        output = self.root / 'output'
        with patch.object(sys, 'argv', ['service.py']), patch.object(service, 'load_config', return_value=self.cfg), patch.object(service, 'cmd_run', side_effect=StateError('state')), patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
            self.assertEqual(service.main(), 3)
        self.assertIn('failure_kind=state', output.read_text())


if __name__ == '__main__':
    unittest.main()
