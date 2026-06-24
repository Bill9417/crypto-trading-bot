#!/bin/bash

# Navigate to the app directory first
cd "$(dirname "${BASH_SOURCE[0]}")/app"

# Pre-flight tests BEFORE killing the running server, so a red suite never takes
# the web UI down with nothing to replace it. Bypass with SKIP_TESTS=1.
./preflight.sh || exit 1

# Kill any existing process on port 4000 to prevent "Address already in use" errors
echo "Stopping any existing web server on port 4000..."
lsof -ti:4000 | xargs kill -9 2>/dev/null

echo "Starting Crypto Scanner Web UI on port 4000..."

# Automatically open the web browser after 1.5 seconds
(sleep 1.5 && open http://127.0.0.1:4000) &

# Run the app using the correct Python interpreter
/Users/wolfman/miniforge3/bin/python app.py
