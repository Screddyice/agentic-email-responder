#!/usr/bin/env bash
export XDG_RUNTIME_DIR=/run/user/1000
set -a
source /home/ubuntu/.openclaw/.env
set +a
/usr/bin/python3 /home/ubuntu/scripts/email-responder.py
