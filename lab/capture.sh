#!/bin/sh
set -eu

printf '%s\n' "$$" > /tmp/netrunner-capture.pid
cleanup() {
    if [ -n "${capture_pid:-}" ]; then
        su-exec nobody kill -TERM "$capture_pid" 2>/dev/null || true
        wait "$capture_pid" 2>/dev/null || true
    fi
    rm -f /tmp/netrunner-capture.pid
}
trap cleanup EXIT
trap 'exit 143' INT TERM

tcpdump --immediate-mode -i eth0 -nn -U -Z nobody -w "$1" 'tcp port 80 or tcp port 443 or port 53' > /tmp/netrunner-tcpdump.log 2>&1 &
capture_pid=$!
sleep 1
su-exec nobody kill -0 "$capture_pid" || { cat /tmp/netrunner-tcpdump.log >&2; exit 1; }
su-exec nobody curl --noproxy '*' --max-time 5 -sS -o /dev/null "$2" || true
sleep 1
su-exec nobody kill -TERM "$capture_pid"
wait "$capture_pid"
capture_pid=
su-exec nobody tcpdump --number -nn -r "$1"
