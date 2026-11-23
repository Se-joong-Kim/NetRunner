# 다이제스트로 고정한다(#26). 올릴 때는 docker pull 후 RepoDigests 값을 바꾼다.
FROM alpine:3.22@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f
RUN apk add --no-cache bind-tools busybox-extras ca-certificates curl dnsmasq iproute2 iptables iputils-tracepath nginx openssl su-exec tcpdump && mkdir -p /www && chown nobody:nogroup /www
COPY start.sh capture.sh stop-capture.sh load.sh stop-load.sh make-certs.sh nginx.conf /
RUN sh /make-certs.sh
ENTRYPOINT ["/bin/sh", "/start.sh"]
