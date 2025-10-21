#!/bin/bash

# Check Python version
PYTHON_VERSION=$(python3 -c 'import sys; print(sys.version_info.major)')
if [ "$PYTHON_VERSION" -ge 13 ]; then
    echo "❌ Python $PYTHON_VERSION detected. This script supports only Python versions below 13."
    exit 1
fi

echo "🚀 Starting app.py with Python $PYTHON_VERSION..."

# Run the app with unbuffered output
python3 -u app.py