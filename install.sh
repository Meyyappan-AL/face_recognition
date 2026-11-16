#!/bin/bash

# Check Python version
PYTHON_VERSION=$(python3 -c 'import sys; print(sys.version_info.major)')
if [ "$PYTHON_VERSION" -ge 13 ]; then
    echo "Python $PYTHON_VERSION detected. This script supports only Python versions below 13."
    exit 1
fi

echo "Python version $PYTHON_VERSION is compatible."

# Upgrade pip
python3 -m pip install --upgrade pip

# Install required packages (mediapipe is optional; Haar fallback is built in)
python3 -m pip install -r requirements.txt
echo "All required Python packages have been installed successfully."
