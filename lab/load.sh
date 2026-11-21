#!/bin/sh
set -eu

printf '%s\n' "$$" > /tmp/netrunner-load.pid
trap 'rm -f /tmp/netrunner-load.pid' EXIT
trap 'exit 143' INT TERM
while :; do
    curl --noproxy '*' --max-time 5 -sS -o /dev/null http://10.20.0.11:8000/large || true
done
