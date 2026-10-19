#!/bin/zsh
# Use zsh explicitly: do not depend on Make's default /bin/sh or shell activation.
emulate -L zsh
set -eu
cd -- "${0:A:h:h}"
if [[ ! -f .env ]]; then
  print -u2 'Missing .env. Copy .env.example to .env and configure it first.'
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  print -u2 'Missing .venv. Run python3 -m venv .venv and install requirements.txt first.'
  exit 1
fi
set -a
source ./.env
set +a
exec .venv/bin/python scripts/dev.py
