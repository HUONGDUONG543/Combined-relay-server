#!/bin/sh
# Secret COOKIES_TXT (dan noi dung cookies.txt) -> ghi ra file cho server doc
if [ -n "$COOKIES_TXT" ]; then
  printf '%s\n' "$COOKIES_TXT" > /app/cookies.txt
fi
exec python combined_relay_server.py
