"""On-chain execution layer (Robinhood Chain / Uniswap).

Split by responsibility so the exchange client stays a thin adapter:

``tokens``       symbol <-> (address, decimals) registry
``dexscreener``  cheap price watcher + pool health metrics
``risk``         pre-trade gates, absolute and relative to when a level was armed
``intents``      synthetic-limit-order state machine
``candles``      price samples -> OHLC the grid engine can read as klines

Execution (``chain``, ``uniswap``, ``approvals``, ``receipts``) lands in the
stages that follow; nothing here reads a private key.
"""
