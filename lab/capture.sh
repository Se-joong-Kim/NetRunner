#!/bin/sh
set -eu

printf '%s\n' "$$" > /tmp/netrunner-capture.pid
cleanup() {
    if [ -n "${capture_pid:-}" ]; then
        kill -TERM "$capture_pid" 2>/dev/null || true
        wait "$capture_pid" 2>/dev/null || true
    fi
    rm -f /tmp/netrunner-capture.pid
}
trap cleanup EXIT
trap 'exit 143' INT TERM

tcpdump --immediate-mode -i eth0 -nn -U -w "$1" 'tcp port 80 or port 53' > /tmp/netrunner-tcpdump.log 2>&1 &
capture_pid=$!
sleep 1
kill -0 "$capture_pid" || { cat /tmp/netrunner-tcpdump.log >&2; exit 1; }
curl --noproxy '*' --max-time 5 -sS -o /dev/null "$2" || true
sleep 1
kill -TERM "$capture_pid"
wait "$capture_pid"
capture_pid=
tcpdump --number -nn -r "$1"
