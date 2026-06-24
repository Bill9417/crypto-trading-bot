#!/bin/bash
# Run the strategy test suite with the project's Python interpreter.
cd "$(dirname "$0")"
/Users/wolfman/miniforge3/bin/python -m pytest "$@"
