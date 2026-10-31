import queue
import threading

from brain.domain.events import Event

_SUBSCRIBER_BACKLOG = 256


class _QueueSubscription:
    def __init__(self, bus: InMemoryEventBus) -> None:
        self._bus = bus
        self._queue: queue.Queue[Event] = queue.Queue(maxsize=_SUBSCRIBER_BACKLOG)

    def offer(self, event: Event) -> None:
        """A stalled client loses its oldest events rather than growing memory without bound."""
        while True:
            try:
                self._queue.put_nowait(event)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass

    def next(self, timeout: float) -> Event | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._bus.unsubscribe(self)


class InMemoryEventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[_QueueSubscription] = set()

    def publish(self, event: Event) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            subscriber.offer(event)

    def subscribe(self) -> _QueueSubscription:
        subscription = _QueueSubscription(self)
        with self._lock:
            self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: _QueueSubscription) -> None:
        with self._lock:
            self._subscribers.discard(subscription)
