import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import reply_delivery as delivery
import usage_summary as usage

PRICES = usage.read_json(Path(__file__).with_name('api-pricing.json'))
START = '2026-10-15T09:00:00+00:00'


class UsageTests(unittest.TestCase):
    def test_cache_ttl_and_search_price(self):
        record = {'provider': 'claude', 'model': 'claude-opus-5-5', 'usage': {
            'input_tokens': 1000, 'output_tokens': 100, 'cache_read_input_tokens': 2000,
            'cache_creation_input_tokens': 500, 'cache_creation': {
                'ephemeral_1h_input_tokens': 200, 'ephemeral_5m_input_tokens': 300},
            'server_tool_use': {'web_search_requests': 1}}}
        tokens, usd = usage.price(record, PRICES)
        self.assertEqual(tokens, 3600)
        self.assertAlmostEqual(usd, .004 + .002 + .0004 + .0016 + .0015 + .01)
        del record['usage']['cache_creation']
        self.assertIsNone(usage.price(record, PRICES)[1])

    def test_codex_cache_reasoning_and_long_context(self):
        record = {'provider': 'codex', 'model': 'gpt-6-astra', 'usage': {
            'input_tokens': 300000, 'cached_input_tokens': 200000,
            'cache_write_input_tokens': 10000, 'output_tokens': 1000,
            'reasoning_output_tokens': 800}}
        tokens, usd = usage.price(record, PRICES)
        self.assertEqual(tokens, 301000)
        self.assertAlmostEqual(usd, (90000*10 + 200000 + 10000*12.5)*2/1e6 + .075)

    def test_dedup_and_timezone(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)/'session.jsonl'
            event = {'type': 'token_usage_record', 'timestamp': '2026-10-15T12:01:00+03:00',
                     'payload': {'response_id': 'response-one', 'usage': {'input_tokens': 100, 'output_tokens': 5}}}
            compact = dict(event, payload={'response_id': 'compact-response', 'usage': {'input_tokens': 10, 'output_tokens': 2}})
            p.write_text('\n'.join(map(json.dumps, [
                {'type': 'turn_context', 'payload': {'model': 'gpt-6-astra'}}, event, event, compact])))
            records = usage.codex_records([p,p])
            self.assertEqual(len(records), 2)
            result = usage.summary(records, PRICES, START)
            self.assertEqual(result['turn_tokens'], 117)
            self.assertEqual(result['task_tokens'], 117)
            self.assertFalse(result['incomplete'])
            text = usage.footer('GPT-6 Astra · effort: medium', result, START, '2026-10-15T09:02:03Z')
            self.assertIn('2 мин 03 с', text)

    def test_retired_sessions_and_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = str((root/'work').resolve())
            project = root/'.claude/projects'/workdir.replace('/', '-').replace('.', '-')
            event = lambda key: {'type': 'assistant', 'timestamp': START, 'message': {
                'id': key, 'model': 'claude-opus-5-5', 'usage': {'input_tokens': 1, 'output_tokens': 1}}}
            for name in ['old.jsonl', 'new.jsonl', 'new/subagents/child.jsonl']:
                p = project/name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(event(name))+'\n')
            codex = root/'codex/sessions'
            codex.mkdir(parents=True)
            for sid, source in [('native', 'cli'), ('child', {'subagent': {'thread_spawn': {'parent_thread_id': 'native'}}})]:
                (codex/f'{sid}.jsonl').write_text('\n'.join(map(json.dumps, [
                    {'type': 'session_meta', 'payload': {'id': sid, 'source': source}},
                    {'type': 'turn_context', 'payload': {'model': 'gpt-6-astra'}},
                    {'type': 'token_usage_record', 'timestamp': START, 'payload': {'response_id': sid, 'usage': {'input_tokens': 2}}} ])))
            state = {'session_id': 'new', 'sessions': [{'id': 'old'}], 'codex_session_id': 'native'}
            records, missing = usage.task_records(state, 'task', workdir, root, root/'codex')
            self.assertFalse(missing)
            self.assertEqual(len(records), 5)
            (project/'old.jsonl').unlink()
            self.assertTrue(usage.task_records(state, 'task', workdir, root, root/'codex')[1])

    def test_queue_last_reply_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'replies/turn.json'
            usage.write_json(root/'turns/task.json', {'single_reply': True, 'reply_path': str(path)})
            self.assertTrue(delivery.queue(root, 'task', {'text': 'Starting'}))
            self.assertTrue(delivery.queue(root, 'task', {'text': 'Done'}))
            send = Mock(return_value=True)
            self.assertFalse(delivery.deliver(path, send))
            send.assert_not_called()
            entry = usage.read_json(path)
            self.assertEqual(entry['body']['text'], 'Done')
            entry['ready'] = True
            usage.write_json(path, entry)
            send.return_value = False
            self.assertFalse(delivery.deliver(path, send))
            send.return_value = True
            self.assertTrue(delivery.deliver(path, send))
            self.assertTrue(delivery.deliver(path, send))
            self.assertEqual(send.call_count, 2)




if __name__ == '__main__':
    unittest.main()
