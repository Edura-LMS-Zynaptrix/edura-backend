"""Message-bus client used by every service: publish events, run a consumer thread.

Services do not call each other directly. They publish events to the shared
``edura.events`` topic exchange and consume the ones they care about from their
own durable queue (see ``shared/events.py`` for the catalogue).

Delivery is at-least-once, so handlers must be idempotent: the replica updates in
``shared/replicas.py`` are all "set to this state" upserts and are safe to repeat.
"""

import json
import logging
import os
import threading
import time
from typing import Callable

import pika

from shared.events import EXCHANGE

logger = logging.getLogger(__name__)

Handler = Callable[[str, dict], None]

_publish_lock = threading.Lock()
_connection: pika.BlockingConnection | None = None
_channel = None
_broker_down_until = 0.0
# After a failed publish, skip the broker for a few seconds so a broker outage
# slows each request by milliseconds, not by a connection timeout.
_BACKOFF_SECONDS = 5.0


def _parameters(attempts: int = 1, heartbeat: int = 60) -> pika.ConnectionParameters:
    return pika.ConnectionParameters(
        host=os.getenv("RABBITMQ_HOST", "rabbitmq"),
        port=int(os.getenv("RABBITMQ_PORT", "5672")),
        credentials=pika.PlainCredentials(
            os.getenv("RABBITMQ_USER", "guest"),
            os.getenv("RABBITMQ_PASSWORD", "guest"),
        ),
        heartbeat=heartbeat,
        socket_timeout=5,
        blocked_connection_timeout=10,
        connection_attempts=attempts,
        retry_delay=2,
    )


def _open_channel():
    """The cached publishing channel, (re)connected when needed. Caller holds the lock."""
    global _connection, _channel
    if _connection is None or _connection.is_closed or _channel is None:
        # A publisher sits idle between events and is not serviced then, so a
        # heartbeat would only get it dropped by the broker. A dead connection is
        # noticed when the next publish is not confirmed, and replaced.
        _connection = pika.BlockingConnection(_parameters(heartbeat=0))
        _channel = _connection.channel()
        _channel.exchange_declare(
            exchange=EXCHANGE, exchange_type="topic", durable=True
        )
        # The broker confirms every message, so a half-dead connection is noticed
        # at publish time instead of silently losing the event.
        _channel.confirm_delivery()
    return _channel


def _drop_connection() -> None:
    global _connection, _channel
    try:
        if _connection is not None and not _connection.is_closed:
            _connection.close()
    except Exception:
        pass
    _connection, _channel = None, None


def publish(routing_key: str, payload: dict) -> bool:
    """Publish one event. True once the broker has confirmed it; never raises."""
    global _broker_down_until
    if time.monotonic() < _broker_down_until:
        return False
    body = json.dumps(payload, default=str)
    properties = pika.BasicProperties(delivery_mode=2, content_type="application/json")
    last_error: Exception | None = None
    with _publish_lock:
        for _ in range(
            2
        ):  # the second try reconnects after an idle connection was dropped
            try:
                _open_channel().basic_publish(
                    exchange=EXCHANGE,
                    routing_key=routing_key,
                    body=body,
                    properties=properties,
                )
                logger.info("Published %s", routing_key)
                return True
            except Exception as exc:
                last_error = exc
                _drop_connection()
        _broker_down_until = time.monotonic() + _BACKOFF_SECONDS
    logger.error("Could not publish %s: %r", routing_key, last_error)
    return False


def _callback(name: str, handler: Handler):
    def on_message(channel, method, properties, body):
        key = method.routing_key
        try:
            data = json.loads(body)
        except ValueError:
            logger.error("[%s] dropped %s: not JSON", name, key)
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return
        for attempt in (1, 2, 3):
            try:
                handler(key, data)
                break
            except Exception as exc:
                logger.error("[%s] %s failed (try %s/3): %r", name, key, attempt, exc)
                if attempt < 3:
                    time.sleep(attempt * 1.5)
        channel.basic_ack(delivery_tag=method.delivery_tag)

    return on_message


def _consume_once(
    name: str, queue: str, bindings: list[str], handler: Handler, prefetch: int
):
    """Connect, bind and consume until the connection drops."""
    connection = pika.BlockingConnection(_parameters(attempts=10))
    try:
        channel = connection.channel()
        channel.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
        channel.queue_declare(queue=queue, durable=True)
        for key in bindings:
            channel.queue_bind(queue=queue, exchange=EXCHANGE, routing_key=key)
        channel.basic_qos(prefetch_count=prefetch)
        channel.basic_consume(queue=queue, on_message_callback=_callback(name, handler))
        logger.info("[%s] listening on %s (%s keys)", name, queue, len(bindings))
        channel.start_consuming()
    finally:
        try:
            if not connection.is_closed:
                connection.close()
        except Exception:
            pass


def start_consumer(
    name: str, queue: str, bindings: list[str], handler: Handler, prefetch: int = 20
) -> threading.Thread:
    """Consume ``bindings`` into ``queue`` on a background thread, calling ``handler(routing_key, data)``.

    Reconnects forever. A message whose handler keeps failing is logged and
    dropped after three tries, so one bad message cannot block the queue; the
    replicas heal at the next resync (see ``resync_later``).
    """

    def run():
        while True:
            try:
                _consume_once(name, queue, bindings, handler, prefetch)
            except Exception as exc:
                logger.warning("[%s] lost the broker: %r; retrying in 5s", name, exc)
                time.sleep(5)

    thread = threading.Thread(target=run, name=f"consumer-{name}", daemon=True)
    thread.start()
    return thread


def resync_later(task: Callable[[], None], delay: float = 15.0) -> None:
    """Re-announce this service's reference data shortly after startup.

    A replica that was empty (a new database, a queue declared after the data
    changed) is filled from this. Events are upserts, so repeating them is safe.
    Turn off with EVENTS_RESYNC_ON_START=0.
    """
    if os.getenv("EVENTS_RESYNC_ON_START", "1") != "1":
        return

    def run():
        time.sleep(delay)
        try:
            task()
        except Exception as exc:
            logger.error("Startup resync failed: %r", exc)

    threading.Thread(target=run, name="resync", daemon=True).start()
