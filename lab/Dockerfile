FROM alpine:3.22
RUN apk add --no-cache bind-tools busybox-extras curl dnsmasq iproute2 iptables iputils-tracepath openssl tcpdump
COPY start.sh capture.sh stop-capture.sh /
ENTRYPOINT ["/bin/sh", "/start.sh"]
