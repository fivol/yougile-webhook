import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

# Must be set before importing app: importing it otherwise talks to YouGile
# (webhook self-heal, sticker lookup) with whatever the local .env holds.
_STATE = tempfile.mkdtemp()
os.environ["YOUGILE_WEBHOOK_URL"] = ""
os.environ["YOUGILE_API_KEY"] = ""
os.environ["STATE_DIR"] = _STATE

import app  # noqa: E402


def _rule(name="card", session_per_chat=True):
    return app.Rule(
        name=name, enabled=True, events={"chat_message-created"}, pattern=None,
        allowed_sender_emails=[], column_names=[], prompt_template="",
        prompt_template_followup="", workdir=_STATE, extra_args=[],
        session_per_chat=session_per_chat,
    )


def _payload(chat_id, text="hi"):
    return {"event": "chat_message-created",
            "payload": {"id": 1, "chatId": chat_id, "text": text}}


class CapacityQueueTests(unittest.TestCase):
    def setUp(self):
        app._capacity_queue.clear()
        app._active_procs.clear()
        self.rule = _rule()
        self.rules = patch.object(app, "RULES", [self.rule])
        self.rules.start()
        self.notice = patch.object(app, "_post_queued_message")
        self.posted = self.notice.start()
        self.kick = patch.object(app, "_kick_capacity_queue")
        self.kick.start()

    def tearDown(self):
        patch.stopall()
        app._capacity_queue.clear()
        app._active_procs.clear()
        app.CAPACITY_QUEUE_FILE.unlink(missing_ok=True)

    def _fill(self):
        app._active_procs.update(range(1, app.CLAUDE_MAX_CONCURRENT + 1))

    def test_over_cap_queues_and_notifies_once(self):
        self._fill()
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        self.assertEqual(app.spawn_claude(self.rule, _payload(a)), (True, "queued-capacity"))
        self.assertEqual(app.spawn_claude(self.rule, _payload(b)), (True, "queued-capacity"))
        # A follow-up while waiting replaces the payload but keeps the place.
        self.assertEqual(app.spawn_claude(self.rule, _payload(a, "newer")),
                         (True, "queued-capacity"))
        self.assertEqual(list(app._capacity_queue), [a, b])
        self.assertEqual(app._capacity_queue[a][1]["payload"]["text"], "newer")
        self.assertEqual([c.args for c in self.posted.call_args_list], [(a, 1), (b, 2)])
        # The chat lock is free again, so a later message can still interrupt-chain.
        self.assertFalse(app.chat_turn_in_flight(a))

    def test_non_chat_rule_over_cap_is_refused(self):
        self._fill()
        rule = _rule(session_per_chat=False)
        ok, why = app.spawn_claude(rule, _payload(str(uuid.uuid4())))
        self.assertEqual((ok, why), (False, "global-max-concurrent"))
        self.assertFalse(app._capacity_queue)

    def test_drain_starts_oldest_first_while_slots_free(self):
        chats = [str(uuid.uuid4()) for _ in range(3)]
        with app._active_lock:
            for c in chats:
                app._enqueue_for_capacity_locked(
                    c, self.rule, _payload(c), {"interrupted": False}, front=False)
        app._active_procs.update(range(1, app.CLAUDE_MAX_CONCURRENT - 1))  # 2 free

        started = []

        def fake_spawn(rule, payload, **kw):
            started.append((app.extract_chat_id(payload), kw))
            app._active_procs.add(100 + len(started))
            return True, "ok"

        with patch.object(app, "spawn_claude", side_effect=fake_spawn):
            app._drain_capacity_queue()
        self.assertEqual([c for c, _ in started], chats[:2])
        self.assertEqual(started[0][1], {"from_queue": True, "interrupted": False})
        self.assertEqual(list(app._capacity_queue), chats[2:])

    def test_drain_stops_when_slot_lost_to_race(self):
        c = str(uuid.uuid4())
        with app._active_lock:
            app._enqueue_for_capacity_locked(c, self.rule, _payload(c), {}, front=False)
        with patch.object(app, "spawn_claude", return_value=(True, "queued-capacity")) as sp:
            app._drain_capacity_queue()
        sp.assert_called_once()

    def test_requeue_from_drain_goes_to_front_without_notice(self):
        self._fill()
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        app.spawn_claude(self.rule, _payload(a))
        app._capacity_queue.pop(a)  # as the drain does before spawning
        app.spawn_claude(self.rule, _payload(b))
        self.posted.reset_mock()
        app.spawn_claude(self.rule, _payload(a), from_queue=True)
        self.assertEqual(list(app._capacity_queue), [a, b])
        self.posted.assert_not_called()

    def test_queue_survives_restart(self):
        c = str(uuid.uuid4())
        with app._active_lock:
            app._enqueue_for_capacity_locked(
                c, self.rule, _payload(c), {"allow_session_recovery": False}, front=False)
        app._capacity_queue.clear()
        app._load_capacity_queue()
        rule, payload, kwargs = app._capacity_queue[c]
        self.assertIs(rule, self.rule)
        self.assertEqual(kwargs, {"allow_session_recovery": False})

    def test_restart_drops_entries_for_removed_rules(self):
        c = str(uuid.uuid4())
        with app._active_lock:
            app._enqueue_for_capacity_locked(c, _rule("gone"), _payload(c), {}, front=False)
        app._capacity_queue.clear()
        app._load_capacity_queue()
        self.assertFalse(app._capacity_queue)

    def test_stop_removes_waiting_chat(self):
        c = str(uuid.uuid4())
        with app._active_lock:
            app._enqueue_for_capacity_locked(c, self.rule, _payload(c), {}, front=False)
        self.assertTrue(app._dequeue_capacity(c))
        self.assertFalse(app._dequeue_capacity(c))

    def test_release_chat_kicks_the_queue(self):
        self.kick.stop()
        with patch.object(app, "_kick_capacity_queue") as kick:
            app._release_chat(None, None)
            app._release_chat(str(uuid.uuid4()), None)
        self.assertEqual(kick.call_count, 2)


if __name__ == "__main__":
    unittest.main()
