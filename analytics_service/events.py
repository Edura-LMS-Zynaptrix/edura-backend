"""Analytics only publishes one thing: the progress report to email. The bus client is shared."""

from shared.messaging import publish as publish_event

__all__ = ["publish_event"]
