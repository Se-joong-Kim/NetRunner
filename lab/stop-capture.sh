#!/bin/sh
set -eu

file=/tmp/netrunner-capture.pid
[ -f "$file" ] || exit 0
pid=$(cat "$file")
case "$pid" in
    ''|*[!0-9]*) echo 'Invalid capture PID' >&2; exit 1 ;;
esac
if [ -r "/proc/$pid/cmdline" ] && tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q '/capture.sh'; then
    kill -TERM -- "-$pid" 2>/dev/null || true
    for attempt in 1 2 3 4 5; do
        [ -e "/proc/$pid" ] || { rm -f "$file"; exit 0; }
        sleep 1
    done
    echo 'Capture did not stop' >&2
    exit 1
fi
rm -f "$file"
