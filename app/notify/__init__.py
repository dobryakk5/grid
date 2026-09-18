"""Operations pushed to Telegram: what is worth a message, and how it gets there.

    trading code -> enqueue() -> notifications table -> notifier worker -> Telegram

The split exists so that nothing in the trading path ever waits on, or fails
because of, a messaging API. See :mod:`app.notify.outbox` for why the queue is
a table rather than an in-process buffer.
"""
