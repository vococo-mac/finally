#!/usr/bin/env python3
"""Install uv and run tests."""
import subprocess
import sys
import os

# Try to find uv
result = subprocess.run(
    ["pip", "install", "uv"],
    capture_output=True, text=True
)
print("pip install uv:", result.returncode)
print(result.stdout)
print(result.stderr)

# Try uv sync
os.chdir("/home/runner/work/finally/finally/backend")
result = subprocess.run(
    ["python3", "-m", "uv", "sync", "--extra", "dev"],
    capture_output=True, text=True
)
print("uv sync:", result.returncode)
print(result.stdout)
print(result.stderr)
