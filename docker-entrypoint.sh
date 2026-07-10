#!/bin/sh
set -eu

KEY_FILE="${APP_ENCRYPTION_KEY_FILE:-/app/data/.app_encryption_key}"

if [ -z "${APP_ENCRYPTION_KEY:-}" ]; then
    if [ -s "$KEY_FILE" ]; then
        APP_ENCRYPTION_KEY="$(cat "$KEY_FILE")"
    else
        umask 077
        APP_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
        printf '%s\n' "$APP_ENCRYPTION_KEY" > "$KEY_FILE"
    fi
    export APP_ENCRYPTION_KEY
fi

exec "$@"
