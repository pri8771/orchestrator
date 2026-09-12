"""BOTS-117: no live API calls, real durable state and process-exit regression checks."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'operator/whb_operator.py'
WORKFLOW = ROOT.parent / '.github/workflows/wait-how-big-operator.yml'
spec = importlib.util.spec_from_file_location('whb_release', SOURCE)
whb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(whb)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.durable = self.base / 'durable'; self.durable.mkdir()
        self.state_path = self.durable / 'state.json'
        self.queue_path = self.base / 'queue.json'
        self.config_path = self.base / 'config.json'
        self.plan_path = self.base / 'plan.json'
        self.grant_path = self.base / 'release.grant.json'
        queue = json.loads((ROOT / 'operator/queue.json').read_text())
        queue['queue'] = queue['queue'][:2]
        whb.write_json(self.queue_path, queue)
        self.config_path.write_bytes((ROOT / 'operator/config.json').read_bytes())
        whb.write_json(self.state_path, whb.default_state())
        config = whb.load_json(self.config_path, {})
        self.channels = {s: {'id': config['channels'][s]['id'], 'service': s,
                             'name': config['channels'][s]['handle'], 'isQueuePaused': False}
                         for s in whb.REQUIRED}
        self.histories = {s: [] for s in whb.REQUIRED}
        self.sent = 0
        self.fail = None
        self.patchers = [patch.multiple(whb, QUEUE_PATH=self.queue_path, STATE_PATH=self.state_path,
                                       CONFIG_PATH=self.config_path, PLAN_PATH=self.plan_path),
                         patch.dict(os.environ, {'BUFFER_API_KEY': 'fixture-only', 'WHB_DRY_RUN': 'true',
                                   'WHB_KILL_SWITCH': 'true', 'WHB_DURABLE_STATE_DIR': str(self.durable)}, clear=True),
                         patch.object(whb, 'get_org_and_channels', return_value=('fixture-org', self.channels)),
                         patch.object(whb, 'get_channel_posts', side_effect=self.history),
                         patch.object(whb, 'media_available', return_value=True),
                         patch.object(whb, 'create_video_post', side_effect=self.create)]
        self.patchers.append(patch('sys.stdout', new=io.StringIO()))
        for p in self.patchers: p.start()

    def tearDown(self):
        for p in reversed(self.patchers): p.stop()
        self.tmp.cleanup()

    def history(self, key, org, channel_id):
        service = next(s for s, c in self.channels.items() if c['id'] == channel_id)
        return copy.deepcopy(self.histories[service])

    def create(self, api_key, service, channel_id, text, media_url, due_at, thumbnail):
        # Assert pre-send intent is already on disk, not merely in process memory.
        state = whb.load_json(self.state_path, {})
        self.assertTrue(any(e['state'] == 'intent' for e in state['effects'].values()))
        self.sent += 1
        post = {'id': 'fixture-post-' + str(self.sent), 'channelId': channel_id, 'text': text,
                'assets': [{'source': media_url}], 'dueAt': whb.iso(due_at), 'status': 'scheduled',
                'externalLink': None}
        self.histories[service].append(post)
        if self.fail: raise self.fail
        return post

    def prepare(self):
        self.assertEqual(whb.main(), 0)
        plan = whb.load_json(self.plan_path, {})
        grant = {'authorized': True, 'mode': 'manual_canary', 'max_mutations': 1,
                 'authorization_ref': 'fixture-grant', 'history_review_ref': 'fixture-history-reviewed',
                 'plan_sha256': whb.sha(whb.canonical(plan)), 'binding': plan['binding'],
                 'target': plan['planned'][0]['target'],
                 'expires_at': whb.iso(whb.utc_now() + whb.timedelta(hours=1))}
        whb.write_json(self.grant_path, grant)
        os.environ.update(WHB_DRY_RUN='false', WHB_KILL_SWITCH='false', WHB_MANUAL_CANARY='true',
                          WHB_CANARY_GRANT=str(self.grant_path))
        return plan, grant

    def test_dry_run_with_kill_switch_preserves_actual_state_byte_for_byte(self):
        before = self.state_path.read_bytes()
        self.assertEqual(whb.main(), 0)
        self.assertEqual(before, self.state_path.read_bytes())
        plan = whb.load_json(self.plan_path, {})
        self.assertEqual(plan['actual_mutations'], 0)
        self.assertTrue(plan['publication_held'])
        self.assertEqual(len(plan['planned']), 6)
        self.assertNotIn('post_id', plan['planned'][0])
        self.assertNotIn('scheduled', json.dumps(plan['planned']))
        self.assertIsNone(whb.load_json(self.state_path, {})['anchor_utc'])
        self.assertEqual(self.sent, 0)

    def test_paused_actual_state_still_allows_read_only_bootstrap(self):
        state = whb.load_json(self.state_path, {}); state['paused'] = True
        whb.write_json(self.state_path, state); before = self.state_path.read_bytes()
        self.assertEqual(whb.main(), 0)
        self.assertEqual(before, self.state_path.read_bytes())

    def test_normal_github_publish_refused_even_with_grant_and_local_path(self):
        self.prepare(); os.environ['GITHUB_ACTIONS'] = 'true'
        self.assertEqual(whb.main(), 2)
        self.assertEqual(self.sent, 0)

    def test_normal_run_requires_durable_state_and_explicit_canary(self):
        self.prepare(); os.environ.pop('WHB_DURABLE_STATE_DIR')
        self.assertEqual(whb.main(), 2)
        os.environ['WHB_DURABLE_STATE_DIR'] = str(self.durable)
        os.environ.pop('WHB_MANUAL_CANARY')
        self.assertEqual(whb.main(), 2)
        self.assertEqual(self.sent, 0)

    def test_arbitrary_bootstrap_timestamp_cannot_authorize_effect(self):
        self.prepare(); self.plan_path.unlink()
        state = whb.load_json(self.state_path, {})
        state['bootstrap'] = {'successful_dry_run_utc': whb.iso(whb.utc_now())}
        whb.write_json(self.state_path, state)
        with self.assertRaisesRegex(RuntimeError, 'bootstrap'): whb.main()
        self.assertEqual(self.sent, 0)

    def test_queue_config_channels_and_source_binding_drift_rejected(self):
        plan, grant = self.prepare()
        for field in ('queue_sha256', 'config_sha256', 'channels_sha256', 'source_sha256'):
            modified = copy.deepcopy(plan); modified['binding'][field] = 'wrong'
            whb.write_json(self.plan_path, modified)
            with self.assertRaisesRegex(RuntimeError, 'bootstrap'): whb.main()
        whb.write_json(self.plan_path, plan)
        queue = whb.load_json(self.queue_path, {}); queue['queue'][0]['captions']['twitter'] += '!'
        whb.write_json(self.queue_path, queue)
        with self.assertRaisesRegex(RuntimeError, 'bootstrap'): whb.main()
        self.assertEqual(self.sent, 0)

    def test_exact_grant_plan_hash_expiry_and_target_are_enforced(self):
        plan, grant = self.prepare()
        for change in [{'plan_sha256': 'wrong'}, {'expires_at': '2000-01-01T00:00:00Z'},
                       {'target': 'absent'}, {'max_mutations': 3}, {'history_review_ref': None}]:
            whb.write_json(self.grant_path, grant | change)
            with self.assertRaises(RuntimeError): whb.main()
        self.assertEqual(self.sent, 0)

    def test_unknown_channel_pause_state_cannot_publish(self):
        self.channels['twitter'].pop('isQueuePaused')
        self.prepare()
        with self.assertRaisesRegex(RuntimeError, 'paused'): whb.main()
        self.assertEqual(self.sent, 0)

    def test_success_creates_one_verified_buffer_receipt_and_replay_is_zero_effect(self):
        plan, grant = self.prepare()
        self.assertEqual(whb.main(), 0)
        state = whb.load_json(self.state_path, {})
        self.assertEqual(state['effects'][grant['target']]['state'], 'verified_buffer')
        self.assertEqual(state['anchor_utc'], plan['proposed_anchor_utc'])
        self.assertEqual(whb.main(), 0)
        self.assertEqual(self.sent, 1)

    def test_timeout_is_uncertain_and_never_automatically_resent(self):
        plan, grant = self.prepare(); self.fail = TimeoutError()
        self.assertEqual(whb.main(), 2)
        self.assertEqual(whb.main(), 2)
        self.assertEqual(self.sent, 1)
        state = whb.load_json(self.state_path, {})
        self.assertEqual(state['effects'][grant['target']]['state'], 'uncertain')
        self.assertEqual(state['posts'], {})
        os.environ['WHB_RECONCILE'] = 'true'
        self.assertEqual(whb.main(), 0)
        self.assertEqual(self.sent, 1)

    def test_inconclusive_reconciliation_does_not_clear_intent(self):
        plan, grant = self.prepare(); self.fail = TimeoutError(); whb.main()
        self.histories = {s: [] for s in whb.REQUIRED}; os.environ['WHB_RECONCILE'] = 'true'
        self.assertEqual(whb.main(), 2)
        self.assertEqual(whb.load_json(self.state_path, {})['effects'][grant['target']]['state'], 'uncertain')
        self.assertEqual(self.sent, 1)

    def test_same_caption_or_media_alone_never_counts_as_exact_receipt(self):
        queue = whb.load_json(self.queue_path, {})['queue']
        self.histories['twitter'] = [{'id': 'foreign', 'channelId': self.channels['twitter']['id'],
                                    'text': queue[0]['captions']['twitter'], 'assets': [{'source': 'different'}]}]
        with self.assertRaisesRegex(RuntimeError, 'Conflicting'): whb.main()
        self.assertEqual(whb.load_json(self.state_path, {})['posts'], {})

    def test_media_failure_does_not_create_successful_plan(self):
        with patch.object(whb, 'media_available', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'Media unavailable'): whb.main()
        self.assertFalse(self.plan_path.exists())
        self.assertEqual(self.sent, 0)

    def test_legacy_dry_run_fake_actuals_fail_closed(self):
        state = whb.default_state(); state['posts'] = {'WHB-000': {'twitter': {'post_id': 'dry-run'}}}
        whb.write_json(self.state_path, state)
        with self.assertRaisesRegex(RuntimeError, 'synthetic'): whb.main()

    def test_stale_process_lock_is_not_automatically_reclaimed(self):
        self.prepare(); (self.durable / 'operator.lock').write_text('old process, must inspect')
        with self.assertRaisesRegex(RuntimeError, 'State lock'): whb.main()
        self.assertEqual(self.sent, 0)

    def test_tampered_plan_cannot_redirect_valid_grant_outside_queue_or_channel(self):
        plan, grant = self.prepare()
        plan['planned'][0]['payload']['channel_id'] = 'unrelated-account'
        plan['planned'][0]['payload_sha256'] = whb.sha(whb.canonical(plan['planned'][0]['payload']))
        grant['plan_sha256'] = whb.sha(whb.canonical(plan))
        whb.write_json(self.plan_path, plan); whb.write_json(self.grant_path, grant)
        with self.assertRaisesRegex(RuntimeError, 'exact queue/channel'): whb.main()
        self.assertEqual(self.sent, 0)

    def test_explicit_proposed_anchor_replans_without_overwriting_actual_anchor(self):
        state = whb.load_json(self.state_path, {}); state['anchor_utc'] = '2020-01-01T00:00:00Z'
        whb.write_json(self.state_path, state); before = self.state_path.read_bytes()
        os.environ['WHB_PROPOSED_ANCHOR_UTC'] = whb.iso(whb.utc_now() + whb.timedelta(hours=1))
        self.assertEqual(whb.main(), 0)
        self.assertEqual(before, self.state_path.read_bytes())

    def test_real_process_exit_preserves_intent_before_mutation(self):
        plan, grant = self.prepare()
        program = '''import importlib.util,json,os
from pathlib import Path
spec=importlib.util.spec_from_file_location("whb_child",os.environ["FIXTURE_SOURCE"])
w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
base=Path(os.environ["FIXTURE_BASE"])
w.QUEUE_PATH=base/"queue.json";w.CONFIG_PATH=base/"config.json";w.PLAN_PATH=base/"plan.json"
channels=json.loads(os.environ["FIXTURE_CHANNELS"])
w.get_org_and_channels=lambda key:("fixture-org",channels)
w.get_channel_posts=lambda *args:[]
w.media_available=lambda url:True
w.create_video_post=lambda *args:os._exit(17)
w.main()
'''
        env = dict(os.environ, FIXTURE_SOURCE=str(SOURCE), FIXTURE_BASE=str(self.base),
                   FIXTURE_CHANNELS=json.dumps(self.channels))
        proc = subprocess.run([sys.executable, '-c', program], env=env, capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 17, proc.stderr)
        self.assertEqual(whb.load_json(self.state_path, {})['effects'][grant['target']]['state'], 'intent')
        self.assertTrue((self.durable / 'operator.lock').exists())
        with self.assertRaisesRegex(RuntimeError, 'State lock'): whb.main()


class BundleTests(unittest.TestCase):
    def test_organization_selection_uses_exact_existing_channels_not_first_org(self):
        config = json.loads((ROOT / 'operator/config.json').read_text())
        exact = [{'id': config['channels'][s]['id'], 'service': s, 'isQueuePaused': False}
                 for s in whb.REQUIRED]
        responses = [{'account': {'organizations': [{'id': 'unrelated'}, {'id': 'correct'}]}},
                     {'channels': [{'id': 'other-id', 'service': 'twitter'}]}, {'channels': exact}]
        with patch.object(whb, 'gql', side_effect=responses):
            org, channels = whb.get_org_and_channels('fixture-only')
        self.assertEqual(org, 'correct')
        self.assertEqual(set(channels), set(whb.REQUIRED))
        with patch.object(whb, 'gql', side_effect=[responses[0], {'channels': exact}, {'channels': exact}]):
            with self.assertRaisesRegex(RuntimeError, 'not uniquely'): whb.get_org_and_channels('fixture-only')

    def test_rebuilding_plain_sources_is_byte_reproducible(self):
        build_spec = importlib.util.spec_from_file_location('build_whb', ROOT / 'operator/build_bundle.py')
        build = importlib.util.module_from_spec(build_spec); build_spec.loader.exec_module(build)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); operator = base / 'operator'; operator.mkdir()
            workflow = base / 'workflow.yml'; workflow.write_bytes(WORKFLOW.read_bytes())
            for name in build.MEMBERS: (operator / name).write_bytes((ROOT / 'operator' / name).read_bytes())
            with patch.multiple(build, ROOT=operator, WORKFLOW=workflow), patch('sys.stdout', new=io.StringIO()):
                first = build.build(); second = build.build()
            self.assertEqual(first, second)
            self.assertEqual((operator / 'operator_bundle.zip').read_bytes(), (ROOT / 'operator/operator_bundle.zip').read_bytes())

    def test_plain_source_matches_every_zip_member(self):
        with zipfile.ZipFile(ROOT / 'operator/operator_bundle.zip') as bundle:
            self.assertEqual(sorted(bundle.namelist()), ['config.json', 'queue.json', 'whb_operator.py'])
            for name in bundle.namelist(): self.assertEqual(bundle.read(name), (ROOT / 'operator' / name).read_bytes())
        digest = hashlib.sha256((ROOT / 'operator/operator_bundle.zip').read_bytes()).hexdigest()
        self.assertIn(digest, WORKFLOW.read_text())

    def test_packaged_executable_does_not_shadow_operator_and_no_key_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(ROOT / 'operator/operator_bundle.zip') as bundle: bundle.extractall(tmp)
            proc = subprocess.run([sys.executable, str(Path(tmp) / 'whb_operator.py')], cwd=tmp,
                                  env={}, capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('WAIT_HOW_BIG_NOT_CONFIGURED', proc.stdout)
            check = subprocess.run([sys.executable, '-c', 'from operator import eq; assert eq(1,1)'],
                                   cwd=tmp, env={}, capture_output=True, timeout=10)
            self.assertEqual(check.returncode, 0)

    def test_workflow_is_read_only_and_has_no_post_effect_git_state_persistence(self):
        workflow = WORKFLOW.read_text()
        self.assertIn('contents: read', workflow)
        self.assertIn('default: true', workflow)
        self.assertIn("github.event_name == 'schedule' || inputs.dry_run", workflow)
        self.assertNotIn('\n          git push', workflow)
        self.assertNotIn('/operator.py', workflow)

    def test_queue_preserves_all_thirteen_existing_media_and_channel_ids(self):
        queue = json.loads((ROOT / 'operator/queue.json').read_text())
        self.assertEqual(len(queue['queue']), 13)
        config = json.loads((ROOT / 'operator/config.json').read_text())
        self.assertEqual({s:c['id'] for s,c in config['channels'].items()}, {
            'twitter':'6a8f2926ccaf649a671fa86d', 'instagram':'6a8f1d89ccaf649a671f69fc',
            'tiktok':'6a8fb214ccaf649a6724d002'})


if __name__ == '__main__': unittest.main()
