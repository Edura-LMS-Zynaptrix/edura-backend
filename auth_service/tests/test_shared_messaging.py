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


def test_given_broker_down_when_event_published_then_no_error_is_raised(monkeypatch):
    # Arrange
    def refuse(*args, **kwargs):
        raise ConnectionError("no broker")

    monkeypatch.setattr(messaging.pika, "BlockingConnection", refuse)

    # Act and assert
    assert messaging.publish("auth.account-updated", {"user_id": 1}) is False


def test_given_broker_that_just_failed_when_publishing_again_then_it_is_skipped_for_a_few_seconds(
    monkeypatch,
):
    # Arrange
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

    # Act and assert
    assert len(attempts) == tried  # no more connection attempts while backing off
    assert time.monotonic() - start < 1


def test_given_idle_connection_dropped_when_publishing_then_it_reconnects_once(
    monkeypatch,
):
    # Arrange
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

    # Act and assert
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


def test_given_event_when_consumed_then_handler_is_called_and_message_acknowledged():
    # Arrange
    seen = []
    callback = messaging._callback("t", lambda key, data: seen.append((key, data)))

    # Act
    channel, method, body = _message("access.changed", b'{"student_id": 3}')

    callback(channel, method, None, body)

    # Assert
    assert seen == [("access.changed", {"student_id": 3})]
    channel.basic_ack.assert_called_once_with(delivery_tag=7)


def test_given_message_that_is_not_json_when_consumed_then_it_is_dropped_without_retry():
    # Arrange
    handler = MagicMock()
    callback = messaging._callback("t", handler)

    # Act
    channel, method, body = _message("access.changed", b"not json")
    callback(channel, method, None, body)

    # Assert
    handler.assert_not_called()
    channel.basic_ack.assert_called_once()


def test_given_failing_handler_when_event_consumed_then_tried_three_times_and_dropped(
    monkeypatch,
):
    # Arrange
    monkeypatch.setattr(messaging.time, "sleep", lambda s: None)

    handler = MagicMock(side_effect=RuntimeError("database asleep"))
    callback = messaging._callback("t", handler)

    # Act
    channel, method, body = _message("course.upserted", b'{"id": 1}')
    callback(channel, method, None, body)

    # Assert
    assert handler.call_count == 3
    channel.basic_ack.assert_called_once()  # a poison message must not block the queue


def test_given_handler_that_recovers_when_event_consumed_then_it_is_not_called_again(
    monkeypatch,
):
    # Arrange
    monkeypatch.setattr(messaging.time, "sleep", lambda s: None)

    # Act
    handler = MagicMock(side_effect=[RuntimeError("blip"), None])
    callback = messaging._callback("t", handler)
    channel, method, body = _message("course.upserted", b'{"id": 1}')
    callback(channel, method, None, body)

    # Assert
    assert handler.call_count == 2


def test_given_resync_switched_off_when_service_starts_then_no_resync_runs(monkeypatch):
    # Arrange
    monkeypatch.setenv("EVENTS_RESYNC_ON_START", "0")
    called = []
    messaging.resync_later(lambda: called.append(1), delay=0)

    # Act
    time.sleep(0.05)

    # Assert
    assert called == []


def test_given_service_queues_when_bindings_checked_then_each_queue_listens_to_something_and_nothing_twice():
    # Arrange
    for queue, keys in ev.QUEUES.items():
        assert keys, queue
        assert len(keys) == len(set(keys)), f"{queue} binds a key twice"

    # Act and assert
    assert set(ev.OBSOLETE_QUEUES).isdisjoint(ev.QUEUES)
