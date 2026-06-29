"""Make the app modules importable from tests (backtest, bot, strategy2_meter, …)
regardless of where pytest is invoked from."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
