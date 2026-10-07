"""Strategy, risk, backtesting and live-trading layers.

Nothing in this package talks to the broker directly: the live trader goes
through the ``Broker`` protocol in ``trading.broker``, so everything here can be
tested and backtested without a cTrader connection.
"""
