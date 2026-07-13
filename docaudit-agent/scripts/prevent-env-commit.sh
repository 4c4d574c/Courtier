#!/usr/bin/env bash
set -euo pipefail
matches=$(git diff --cached --name-only | grep -E '(^|/)\.env($|\.)' | grep -vE '(^|/)\.env\.example$' || true)
if [ -n "$matches" ]; then
    echo "ERROR: .env files must not be committed:"
    echo "$matches"
    exit 1
fi
