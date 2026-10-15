#!/bin/sh
set -eu
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
existing=$(git config --local --get core.hooksPath || true)
if [ -n "$existing" ] && [ "$existing" != ".githooks" ]; then
    echo "Existing hooksPath found; review before replacing: $existing" >&2
    exit 1
fi
chmod +x .githooks/pre-commit .githooks/commit-msg .githooks/pre-push
git config --local core.hooksPath .githooks
echo 'Git hooks installed. Codex hooks separately require review in /hooks.'
