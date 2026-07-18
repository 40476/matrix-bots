#!/bin/bash
# ==============================================================================
# Matrix Based Bot Startup Script
# ==============================================================================
# This script checks if the Python virtual environment (venv) exists,
# manages/installs the required dependencies, and launches the bot.

# Navigate to the directory where this script is located
cd "$(dirname "$0")"

echo "[*] Checking Python environment..."

# Check if the venv directory exists but is broken (missing the 'activate' script)
if [ -d "venv" ] && [ ! -f "venv/bin/activate" ]; then
    echo "[!] Error: 'venv' directory exists but is broken (missing 'activate')."
    echo "[*] Attempting to delete and recreate it..."
    rm -rf venv 2>/dev/null
    if [ -d "venv" ]; then
        echo "[!] ERROR: Could not delete 'venv' because it is owned by another user (root)."
        echo "[*] Please run: 'sudo rm -rf venv' in your terminal, then try again."
        exit 1
    fi
fi

# Create a fresh virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "[*] Creating a fresh virtual environment (venv)..."
    python3 -m venv venv
    if [ $? -ne 0 ]; then
        echo "[!] ERROR: Could not create 'venv'. Make sure 'python3-venv' is installed."
        echo "[*] Run: 'sudo apt update && sudo apt install -y python3-venv'"
        exit 1
    fi
fi

# Activate the virtual environment
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
else
    echo "[!] CRITICAL ERROR: venv/bin/activate not found!"
    exit 1
fi

# Upgrade pip and install all necessary dependencies
echo "[*] Checking and updating dependencies..."
pip install --upgrade pip
pip install matrix-nio httpx pillow bs4 ddgs

# Run the bot
echo "[*] Starting the Matrix Based Bot..."
python3 main.py
