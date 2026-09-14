"""BOTS-117: exercise the Buffer history adapter without network or state writes."""
import copy
import importlib.util
from pathlib import Path
import re
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / 'operator/whb_operator.py'
SPEC = importlib.util.spec_from_file_location('whb_history_adapter', SOURCE)
whb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(whb)

STATUSES = ('scheduled', 'sending', 'needs_approval', 'sent', 'error')
CHANNEL = '6a8f2926ccaf649a671fa86d'
ORG = 'fixture-whb-organization'
# Sanitized public observation: BUFFER_SENT_READBACK.json, 2026-09-13T02:24:28Z.
SENT = {
    'id': '6aa608727da3ee968d09eba9',
    'text': ('A million seconds is 11.6 days.\nA billion seconds is 31.7 years.\n\n'
             'Three extra zeros changed days into decades.'),
    'dueAt': '2026-09-13T02:23:59.731Z',
    'status': 'sent',
    'channelId': CHANNEL,
    'externalLink': 'https://x.com/2092627545267027968/status/2098960869526835492',
    'assets': [{'source': 'https://raw.githubusercontent.com/pri8771/orchestrator/main/'
                'wait-how-big-social/assets/reviewed/WHB-001-million-vs-billion-seconds-'
                '2039fa4c0a3b5fe56ed1351e3b5092e42042dfc5dcfba072580dd76abe6101ec.mp4'}],
}


def empty_history():
    return {f'history_{status}': {'edges': []} for status in STATUSES}


def post(status, identifier):
    value = copy.deepcopy(SENT)
    value.update(status=status, id=identifier)
    if status != 'sent':
        value.update(dueAt='2026-09-14T12:00:00Z', externalLink=None)
    return value


class HistoryAdapterTests(unittest.TestCase):
    def read(self, response):
        with patch.object(whb, 'gql', return_value=copy.deepcopy(response)) as gql:
            result = whb.get_channel_posts('fixture-only', ORG, CHANNEL)
        gql.assert_called_once()
        return result, gql.call_args

    def assert_rejected(self, response):
        with patch.object(whb, 'gql', return_value=copy.deepcopy(response)) as gql:
            with self.assertRaises(RuntimeError):
                whb.get_channel_posts('fixture-only', ORG, CHANNEL)
        gql.assert_called_once()

    def test_observed_sent_and_scheduled_posts_survive_one_single_status_query(self):
        response = empty_history()
        scheduled = post('scheduled', 'fixture-scheduled-post')
        response['history_sent']['edges'] = [{'node': copy.deepcopy(SENT)}]
        response['history_scheduled']['edges'] = [{'node': scheduled}]
        result, call = self.read(response)
        self.assertEqual({p['id']: p for p in result},
                         {SENT['id']: SENT, scheduled['id']: scheduled})
        key, query, variables = call.args
        self.assertEqual(key, 'fixture-only')
        self.assertEqual(variables, {'organizationId': ORG, 'channelId': CHANNEL})
        self.assertCountEqual(re.findall(r'status\s*:\s*\[([^]]*)\]', query), STATUSES)
        for status in STATUSES:
            self.assertRegex(query, rf'history_{status}\s*:\s*posts\s*\(\s*first\s*:\s*100\b')
        self.assertEqual(len(re.findall(r'channelIds\s*:\s*\[\s*\$channelId\s*\]', query)), 5)
        self.assertEqual(len(re.findall(r'field\s*:\s*createdAt\s*,\s*direction\s*:\s*desc', query)), 5)
        self.assertNotIn('mutation', query)

    def test_explicit_empty_edges_for_every_alias_are_valid(self):
        result, _ = self.read(empty_history())
        self.assertEqual(result, [])

    def test_missing_alias_is_not_empty_history(self):
        for status in STATUSES:
            with self.subTest(status=status):
                response = empty_history()
                del response[f'history_{status}']
                self.assert_rejected(response)

    def test_null_or_malformed_alias_and_edges_are_not_empty_history(self):
        for invalid in (None, [], 'invalid', {}, {'edges': None},
                        {'edges': {}}, {'edges': 'invalid'}):
            with self.subTest(alias=invalid):
                response = empty_history()
                response['history_sent'] = invalid
                self.assert_rejected(response)

    def test_null_or_malformed_edges_and_nodes_are_rejected(self):
        for edge in (None, [], 'invalid', {}, {'node': None}, {'node': []},
                     {'node': 'invalid'}, {'node': {}}):
            with self.subTest(edge=edge):
                response = empty_history()
                response['history_sent']['edges'] = [edge]
                self.assert_rejected(response)

    def test_id_channel_and_status_must_match_the_requested_bucket(self):
        invalid_fields = [('id', ''), ('id', '   '), ('id', None), ('id', 123),
                          ('channelId', 'another-channel'), ('channelId', None),
                          ('status', 'scheduled'), ('status', None)]
        for field, value in invalid_fields:
            with self.subTest(field=field, value=value):
                response = empty_history()
                invalid = copy.deepcopy(SENT)
                invalid[field] = value
                response['history_sent']['edges'] = [{'node': invalid}]
                self.assert_rejected(response)

    def test_duplicate_ids_within_or_across_statuses_are_rejected(self):
        response = empty_history()
        response['history_sent']['edges'] = [{'node': copy.deepcopy(SENT)}] * 2
        self.assert_rejected(response)
        response['history_sent']['edges'] = [{'node': copy.deepcopy(SENT)}]
        response['history_scheduled']['edges'] = [{'node': post('scheduled', SENT['id'])}]
        self.assert_rejected(response)

    def test_each_status_accepts_100_posts_but_rejects_101(self):
        response = empty_history()
        for status in STATUSES:
            response[f'history_{status}']['edges'] = [
                {'node': post(status, f'fixture-{status}-{i}')} for i in range(100)]
        result, _ = self.read(response)
        self.assertEqual(len(result), 500)
        response['history_sent']['edges'].append({'node': post('sent', 'fixture-sent-100')})
        self.assert_rejected(response)


if __name__ == '__main__':
    unittest.main()
