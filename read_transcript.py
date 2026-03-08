#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import os
from pathlib import Path

# Set UTF-8 encoding for output
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

debug_dir = Path(__file__).parent / "debug"
if not debug_dir.exists():
    print("Debug directory not found")
    sys.exit(1)

# Find latest transcript file
transcript_files = sorted(debug_dir.glob("transcript_*.txt"), key=lambda x: x.stat().st_mtime, reverse=True)
if not transcript_files:
    print("No transcript files found")
    sys.exit(1)

latest_file = transcript_files[0]
print(f"Reading transcript from: {latest_file}")
print("=" * 60)

try:
    with open(latest_file, 'r', encoding='utf-8') as f:
        content = f.read()
        print(content)
except Exception as e:
    print(f"Error reading file: {e}")

print("=" * 60)
