#!/bin/bash
# ==============================================================================
# Matrix Last.fm Bot (betterFM) Startup Script
# ==============================================================================
# This script manages the virtual environment, installs dependencies for 
# matrix-nio, pillow, requests, and aiohttp, and launches the Last.fm bot.

# Navigate to the directory where this script is located
cd "$(dirname "$0")"

echo "[*] Checking Python environment for betterFM Bot..."

# Check if the venv directory exists but is broken
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

# Upgrade pip and install necessary dependencies for betterFM
echo "[*] Checking and updating dependencies (matrix-nio, pillow, requests, aiohttp)..."
pip install --upgrade pip
# Install required libraries
pip install matrix-nio pillow requests aiohttp

# Run the bot
if [ -f "main.py" ]; then
    echo "[LAUNCHER] Starting the Matrix Last.fm Bot (betterFM)..."
    python3 main.py
else
    echo "[!] ERROR: main.py not found! Please ensure your bot script is named main.py"
    exit 1
fi
