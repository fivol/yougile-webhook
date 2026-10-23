import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_backends as backends
import usage_summary as usage
START = '2026-10-15T09:00:00+00:00'


class BackendTests(unittest.TestCase):
    def test_quota_requires_error_result(self):
        for text in ["You've hit your limit · resets 3pm", 'Usage limit reached', 'Credit balance is too low']:
            self.assertTrue(backends.quota_failure(json.dumps({'type': 'result', 'is_error': True, 'result': text})))
            self.assertFalse(backends.quota_failure(json.dumps({'type': 'result', 'is_error': False, 'result': text})))
        for text in ['Invalid API key', 'API Error: 529 overloaded_error', 'rate_limit_error', 'Permission denied']:
            self.assertFalse(backends.quota_failure(json.dumps({'type': 'result', 'is_error': True, 'result': text})))

    def test_codex_interrupted_session_and_failure(self):
        log = '\n'.join([json.dumps({'type': 'thread.started', 'thread_id': 'native-id'}),
                         json.dumps({'type': 'turn.failed', 'error': {'message': 'quota'}})])
        session, failure = backends.codex_result(log)
        self.assertEqual(session, 'native-id')
        self.assertIn('quota', failure)

    def test_command_resume_keeps_sandbox_and_reply_channel(self):
        cmd = backends.codex_command('codex', 'gpt-6-astra', 'medium', 'native-id', 'python', '/tmp/mcp.py')
        self.assertEqual(cmd[:4], ['codex', 'exec', 'resume', 'native-id'])
        self.assertIn('sandbox_mode="workspace-write"', cmd)
        self.assertEqual(cmd[-1], '-')
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', cmd)

    def test_auto_review_is_explicit_and_keeps_sandbox(self):
        cmd = backends.codex_command('codex', 'gpt-6-astra', 'high', 'native-id',
                                     'python', '/tmp/mcp.py', auto_review=True)
        self.assertEqual(cmd[:5], ['codex', '--approve-for-me', 'exec', 'resume', 'native-id'])
        self.assertNotIn('approval_policy="never"', cmd)
        self.assertIn('sandbox_mode="workspace-write"', cmd)
        self.assertIn('model_reasoning_effort="high"', cmd)

    def test_actual_claude_model(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, 'home', return_value=Path(directory)):
            root = Path(directory)
            (root/'chats').mkdir()
            (root/'chats/task.json').write_text(json.dumps({'agent_workdir': '/tmp/project', 'session_id': 'session', 'agent_effort': 'xhigh'}))
            slug = str(Path('/tmp/project').resolve()).replace('/', '-').replace('.', '-')
            transcript = root/'.claude/projects'/slug/'session.jsonl'
            transcript.parent.mkdir(parents=True)
            transcript.write_text(json.dumps({'type': 'assistant', 'message': {'model': 'claude-opus-5-5'}})+'\n')
            self.assertEqual(backends.reply_model(root, 'task'), 'Opus 5.5')
            self.assertEqual(backends.reply_label(root, 'task'), 'Opus 5.5 · effort: xhigh')
            (root/'chats/task.json').write_text(json.dumps({'active_provider': 'codex', 'codex_model': 'gpt-6-astra', 'agent_effort': 'medium'}))
            self.assertEqual(backends.reply_model(root, 'task'), 'GPT-6 Astra')
            self.assertEqual(backends.reply_label(root, 'task'), 'GPT-6 Astra · effort: medium')


class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # No credentials, API writes or subprocess model calls in these tests.
        import importlib.util
        import shutil
        import sys
        cls.checkout = tempfile.TemporaryDirectory()
        root = Path(cls.checkout.name)
        source = Path(__file__).parent
        for name in ('app.py', 'settings.py', 'config.yaml'):
            shutil.copy(source/name, root/name)
        if (source/'messages').exists():
            shutil.copytree(source/'messages', root/'messages')
        (root/'rules.toml').write_text('rules = []')
        spec = importlib.util.spec_from_file_location('isolated_webhook', root/'app.py')
        app = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = app
        with patch.dict(os.environ, {'YOUGILE_API_KEY': '', 'YOUGILE_WEBHOOK_URL': '',
                                     'STATE_DIR': str(root/'state')}), \
             patch('urllib.request.urlopen', side_effect=OSError('offline test')), \
             patch.dict(sys.modules):
            # The isolated settings, so this checkout's .env, local config and
            # flow spec stay out of the test.
            s_spec = importlib.util.spec_from_file_location('settings', root/'settings.py')
            sys.modules['settings'] = importlib.util.module_from_spec(s_spec)
            s_spec.loader.exec_module(sys.modules['settings'])
            spec.loader.exec_module(app)
        cls.app = app

    @classmethod
    def tearDownClass(cls):
        cls.checkout.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        a = self.app
        self.patches = [patch.object(a, name, self.root/name) for name in ['STATE_DIR', 'CHATS_STATE_DIR', 'TURNS_STATE_DIR']]
        for p in self.patches:
            p.start()
        for name in ['STATE_DIR', 'CHATS_STATE_DIR', 'TURNS_STATE_DIR']:
            getattr(a, name).mkdir()
        self.rule = type('Rule', (), {'name': 'mention', 'workdir': str(self.root)})()
        self.common = [patch.object(a, 'RULES', [self.rule]), patch.object(a, 'CODEX_FALLBACK', True),
                       patch.object(a, '_mark_turn_finished'), patch.object(a, '_release_chat'),
                       patch.object(a, '_record_turn_metrics'), patch.object(a, '_report_dead_turn'),
                       patch.object(a, '_report_silent_turn')]
        for p in self.common:
            p.start()
        a._pending_turns.clear()

    def tearDown(self):
        for p in reversed(self.common+self.patches):
            p.stop()
        self.tmp.cleanup()

    def record(self, output, **extra):
        log = self.root/'turn.log'
        log.write_text(output)
        return dict(chat_id='task', task_id='task', session_id='claude-id', rule='mention',
                    log_path=str(log), payload={'payload': {'text': 'continue'}}, **extra)

    def test_quota_replays_without_finishing_failure_and_persists_cooldown(self):
        a = self.app
        rec = self.record(json.dumps({'type': 'result', 'is_error': True, 'result': "You've hit your limit"}))
        a.finish_turn(rec, 1, 'limit')
        self.assertEqual(a.select_provider(), 'codex')
        self.assertEqual(a._pending_turns['task'][1], rec['payload'])
        a._report_dead_turn.assert_not_called()
        a._report_silent_turn.assert_not_called()
        (a.STATE_DIR/'provider.json').write_text('{"claude_retry_at":0}')
        self.assertEqual(a.select_provider(), 'claude')

    def test_newer_instruction_wins_over_fallback(self):
        a = self.app
        newer = {'payload': {'text': 'stop previous and do this'}}
        a._pending_turns['task'] = (self.rule, newer)
        rec = self.record(json.dumps({'type': 'result', 'is_error': True, 'result': 'Usage limit reached'}))
        a.finish_turn(rec, 1, 'limit')
        self.assertEqual(a._pending_turns['task'][1], newer)

    def test_interruption_never_starts_fallback(self):
        a = self.app
        rec = self.record(json.dumps({'type': 'result', 'is_error': True, 'result': 'Usage limit reached'}), killed=True)
        a.finish_turn(rec, 0, None)
        self.assertEqual(a.select_provider(), 'claude')
        self.assertFalse(a._pending_turns)

    def test_codex_id_survives_adoption_and_no_claude_accounting(self):
        a = self.app
        a.update_chat_state('task', session_id='claude-id')
        rec = self.record(json.dumps({'type': 'thread.started', 'thread_id': 'codex-id'}), provider='codex', killed=True)
        a.finish_turn(rec, 0, None, adopted_dead=True)
        state = a.get_chat_state('task')
        self.assertEqual(state['session_id'], 'claude-id')
        self.assertEqual(state['codex_session_id'], 'codex-id')
        a._record_turn_metrics.assert_not_called()


    def test_spawn_switch_and_return_keep_prompt_and_native_sessions(self):
        a = self.app
        a.update_chat_state('task', session_id='claude-id', session_bootstrapped=True,
                            codex_session_id='codex-id', active_provider='claude')
        transcript = self.root/'claude.jsonl'
        transcript.write_text('{}')
        handoff = self.root/'handoff.md'
        handoff.write_text('existing branch and PR')
        rule = a.Rule(name='mention', enabled=True, events=set(), pattern=None,
                      allowed_sender_emails=[], column_names=[],
                      prompt_template='Standing instructions {chat_history} {handoff}',
                      prompt_template_followup='Follow-up {chat_history}',
                      workdir=str(self.root), extra_args=[])
        payload = {'payload': {'chatId': 'task', 'id': '101', 'text': 'continue current task'}}
        captured = []
        def spawn(rule, cmd, header, callback, **kw):
            captured.append((cmd, Path(a.read_turn('task')['prompt_path']).read_text()))
            return type('Process', (), {'pid': 123})()
        with patch.object(a, 'extract_chat_id', return_value='task'), \
             patch.object(a, 'fetch_chat_messages', return_value=[]), \
             patch.object(a, 'render_chat_history', return_value=('current request', [])), \
             patch.object(a, 'download_message_attachments', return_value={}), \
             patch.object(a, '_transcript_path', return_value=transcript), \
             patch.object(a, 'build_handoff', return_value=handoff), \
             patch.object(a.agent_backends, 'codex_context', return_value='Codex edited file X; unfinished check Y'), \
             patch.object(a, '_spawn_subprocess', side_effect=spawn), \
             patch.object(a, 'set_task_marker'), patch.object(a, '_reap_turn_logs'):
            a.note_claude_quota()
            ok, reason = a.spawn_claude(rule, payload)
            self.assertTrue(ok, reason)
            cmd, prompt = captured[-1]
            self.assertEqual(cmd[:4], [a.CODEX_BIN, 'exec', 'resume', 'codex-id'])
            self.assertIn('Standing instructions', prompt)
            self.assertIn('existing branch and PR', prompt)
            self.assertIn('Codex edited file X', prompt)
            self.assertEqual(a.get_chat_state('task')['session_id'], 'claude-id')
            a._get_chat_lock('task').release()
            a.clear_turn('task')
            (a.STATE_DIR/'provider.json').write_text('{"claude_retry_at":0}')
            ok, reason = a.spawn_claude(rule, payload)
            self.assertTrue(ok, reason)
            cmd, prompt = captured[-1]
            self.assertEqual(cmd[0], a.CLAUDE_BIN)
            self.assertIn('--resume', cmd)
            self.assertEqual(cmd[cmd.index('--resume')+1], 'claude-id')
            self.assertIn('Codex edited file X', prompt)
            self.assertEqual(a.get_chat_state('task')['codex_session_id'], 'codex-id')
            a._get_chat_lock('task').release()

    def test_claude_settings_and_rule_override(self):
        a = self.app
        with patch.object(a, 'CLAUDE_MODEL', 'claude-opus-5-5'), patch.object(a, 'CLAUDE_EFFORT', 'xhigh'):
            self.assertEqual(a.model_args([]), ['--model', 'claude-opus-5-5'])
            self.assertEqual(a.effort_args([]), ['--effort', 'xhigh'])
            for args in [['--model', 'sonnet', '--effort', 'high'], ['--model=sonnet', '--effort=high']]:
                self.assertEqual(a.model_args(args), [])
                self.assertEqual(a.effort_args(args), [])
                self.assertEqual(backends.option_value(args, '--effort'), 'high')

    def test_mcp_adds_model_to_plain_and_html(self):
        import mcp_yougile as mcp
        with patch.object(mcp.agent_backends, 'reply_label', return_value='GPT-6 Astra · effort: medium'), \
             patch.object(mcp, 'api', return_value={'id': '1'}) as api, patch.object(mcp, 'log'):
            mcp.tool_send_task_message({'taskId': 'task', 'text': 'Done', 'textHtml': '<p>Done</p>'})
            body = api.call_args.args[2]
            self.assertTrue(body['text'].startswith('Модель: GPT-6 Astra'))
            self.assertTrue(body['textHtml'].startswith('<p>Модель: GPT-6 Astra · effort: medium</p>'))

    def test_compact_finish_and_no_duplicate(self):
        a = self.app
        rec = self.record('', single_reply=True, reply_path=str(self.root/'reply.json'), started_at=START)
        usage.write_json(Path(rec['reply_path']), {'body': {'text': 'Done', 'textHtml': '<p>Done</p>'}})
        records = {'response': {'provider': 'codex', 'model': 'gpt-6-astra', 'at': START,
                                 'usage': {'input_tokens': 100, 'output_tokens': 10}}}
        with patch.object(a.usage_summary, 'task_records', return_value=(records, False)), \
             patch.object(a, 'ROOT', Path(__file__).parent), \
             patch.object(a.agent_backends, 'reply_label', return_value='GPT-6 Astra · effort: medium'), \
             patch.object(a, '_send_final_body', return_value=True) as send:
            a.finish_turn(rec, 0, None)
            a._publish_compact_reply(rec, self.rule, None)
            self.assertEqual(send.call_count, 1)
            text = send.call_args.args[1]['text']
            self.assertTrue(text.startswith('Done\n\nGPT-6 Astra'))
            self.assertIn('Токены: ход 110 / задача 110', text)
            a._report_silent_turn.assert_not_called()
            a._report_dead_turn.assert_not_called()

    def test_usage_failure_does_not_drop_answer(self):
        a = self.app
        rec = self.record('', single_reply=True, reply_path=str(self.root/'reply.json'), started_at=START)
        usage.write_json(Path(rec['reply_path']), {'body': {'text': 'Done'}})
        with patch.object(a.usage_summary, 'task_records', side_effect=ValueError('bad stats')), \
             patch.object(a, '_send_final_body', return_value=True) as send:
            a.finish_turn(rec, 0, None)
            self.assertIn('Done', send.call_args.args[1]['text'])
            self.assertIn('статистика временно недоступна', send.call_args.args[1]['text'])

    def test_quota_keeps_start_and_suppresses_draft(self):
        a = self.app
        rec = self.record(json.dumps({'type': 'result', 'is_error': True, 'result': 'Usage limit reached'}),
                          single_reply=True, started_at=START)
        with patch.object(a, '_publish_compact_reply') as publish:
            a.finish_turn(rec, 1, 'limit')
            publish.assert_not_called()
            self.assertEqual(a._pending_turns['task'][1]['_agent_turn_started_at'], START)
            a._report_dead_turn.assert_not_called()

    def test_mcp_buffers_without_api_post(self):
        import mcp_yougile as mcp
        with patch.object(mcp.reply_delivery, 'queue', return_value=True) as queue, \
             patch.object(mcp, 'api') as api:
            response = mcp.tool_send_task_message({'taskId': 'task', 'text': 'Done'})
            self.assertTrue(response['queued'])
            self.assertEqual(queue.call_args.args[2]['text'], 'Done')
            api.assert_not_called()

    def test_cancelled_compact_turn_stays_quiet(self):
        a = self.app
        rec = self.record('', single_reply=True, killed=True)
        with patch.object(a, '_publish_compact_reply') as publish:
            a.finish_turn(rec, 0, None)
            publish.assert_not_called()
            a._report_silent_turn.assert_not_called()
            a._report_dead_turn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
