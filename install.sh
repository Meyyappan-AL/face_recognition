#!/bin/bash

# Check Python version
PYTHON_VERSION=$(python3 -c 'import sys; print(sys.version_info.major)')
if [ "$PYTHON_VERSION" -ge 13 ]; then
    echo "❌ Python $PYTHON_VERSION detected. This script supports only Python versions below 13."
    exit 1
fi

echo "✅ Python version $PYTHON_VERSION is compatible."

# Upgrade pip
python3 -m pip install --upgrade pip

# Install required packages
pip install mediapipe
pip install flask
pip install opencv-python
pip install scikit-learn
pip install pandas
echo "✅ All required Python packages have been installed successfully!"
