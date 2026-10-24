"""The shared message-bus client and replica handlers every service relies on (no broker needed)."""

import time
from unittest.mock import MagicMock

import pytest

from shared import events as ev
from shared import messaging


@pytest.fixture(autouse=True)
def fresh_bus(monkeypatch):
    monkeypatch.setattr(messaging, "_broker_down_until", 0.0)
    monkeypatch.setattr(messaging, "_connection", None)
    monkeypatch.setattr(messaging, "_channel", None)


def test_publish_never_raises_when_the_broker_is_down(monkeypatch):
    def refuse(*args, **kwargs):
        raise ConnectionError("no broker")

    monkeypatch.setattr(messaging.pika, "BlockingConnection", refuse)
    assert messaging.publish("auth.account-updated", {"user_id": 1}) is False


def test_a_down_broker_is_skipped_for_a_few_seconds(monkeypatch):
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(1)
        raise ConnectionError("no broker")

    monkeypatch.setattr(messaging.pika, "BlockingConnection", refuse)
    messaging.publish("a.b", {})
    tried = len(attempts)
    start = time.monotonic()
    for _ in range(50):
        assert messaging.publish("a.b", {}) is False
    assert len(attempts) == tried  # no more connection attempts while backing off
    assert time.monotonic() - start < 1


def test_publish_reconnects_once_after_an_idle_connection_dropped(monkeypatch):
    good_channel = MagicMock()
    stale_channel = MagicMock()
    stale_channel.basic_publish.side_effect = ConnectionError("stale")
    connections = []

    def connect(*args, **kwargs):
        conn = MagicMock()
        conn.is_closed = False
        conn.channel.return_value = stale_channel if not connections else good_channel
        connections.append(conn)
        return conn

    monkeypatch.setattr(messaging.pika, "BlockingConnection", connect)
    assert messaging.publish("course.upserted", {"id": 1}) is True
    assert len(connections) == 2
    good_channel.basic_publish.assert_called_once()
    assert (
        good_channel.basic_publish.call_args.kwargs["routing_key"] == "course.upserted"
    )


def _message(key: str, body: bytes):
    channel = MagicMock()
    method = MagicMock(routing_key=key, delivery_tag=7)
    return channel, method, body


def test_a_consumer_hands_the_event_to_its_handler_and_acks():
    seen = []
    callback = messaging._callback("t", lambda key, data: seen.append((key, data)))
    channel, method, body = _message("access.changed", b'{"student_id": 3}')
    callback(channel, method, None, body)
    assert seen == [("access.changed", {"student_id": 3})]
    channel.basic_ack.assert_called_once_with(delivery_tag=7)


def test_a_message_that_is_not_json_is_dropped_not_retried():
    handler = MagicMock()
    callback = messaging._callback("t", handler)
    channel, method, body = _message("access.changed", b"not json")
    callback(channel, method, None, body)
    handler.assert_not_called()
    channel.basic_ack.assert_called_once()


def test_a_failing_handler_is_tried_three_times_then_dropped(monkeypatch):
    monkeypatch.setattr(messaging.time, "sleep", lambda s: None)
    handler = MagicMock(side_effect=RuntimeError("database asleep"))
    callback = messaging._callback("t", handler)
    channel, method, body = _message("course.upserted", b'{"id": 1}')
    callback(channel, method, None, body)
    assert handler.call_count == 3
    channel.basic_ack.assert_called_once()  # a poison message must not block the queue


def test_a_handler_that_recovers_is_not_called_again(monkeypatch):
    monkeypatch.setattr(messaging.time, "sleep", lambda s: None)
    handler = MagicMock(side_effect=[RuntimeError("blip"), None])
    callback = messaging._callback("t", handler)
    channel, method, body = _message("course.upserted", b'{"id": 1}')
    callback(channel, method, None, body)
    assert handler.call_count == 2


def test_resync_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("EVENTS_RESYNC_ON_START", "0")
    called = []
    messaging.resync_later(lambda: called.append(1), delay=0)
    time.sleep(0.05)
    assert called == []


def test_every_service_queue_listens_to_something_and_nothing_twice():
    for queue, keys in ev.QUEUES.items():
        assert keys, queue
        assert len(keys) == len(set(keys)), f"{queue} binds a key twice"
    assert set(ev.OBSOLETE_QUEUES).isdisjoint(ev.QUEUES)
