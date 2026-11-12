#!/bin/sh
set -eu

case "$1" in
  router)
    ip route del default 2>/dev/null || true
    exec sleep infinity
    ;;
  client)
    ip route del default 2>/dev/null || true
    ip route replace 10.20.0.0/24 via 10.21.0.254
    printf 'nameserver 10.20.0.53\n' > /etc/resolv.conf
    exec sleep infinity
    ;;
  dns)
    ip route del default 2>/dev/null || true
    ip route replace 10.21.0.0/24 via 10.20.0.254
    printf '10.20.0.10 shop.lab\n10.20.0.10 news.lab\n' > /tmp/dns-hosts
    exec dnsmasq --keep-in-foreground --interface=eth0 --bind-interfaces --no-resolv --no-hosts --addn-hosts=/tmp/dns-hosts --pid-file=/run/dnsmasq.pid
    ;;
  web)
    ip route del default 2>/dev/null || true
    ip route replace 10.21.0.0/24 via 10.20.0.254
    iptables -N NETRUNNER
    iptables -I INPUT -j NETRUNNER
    mkdir -p /www
    printf 'NetRunner lab\n' > /www/index.html
    exec httpd -f -p 80 -h /www
    ;;
esac
