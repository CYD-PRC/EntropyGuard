#!/bin/bash
set -e
cd /root/AutoGPT
exec docker compose run --rm autogpt \
  --continuous \
  --skip-reprompt \
  --continuous-limit 25

