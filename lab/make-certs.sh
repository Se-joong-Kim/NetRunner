#!/bin/sh
set -eu
mkdir -p /certs /tmp/ca/newcerts
cd /tmp/ca
: > index.txt
printf 'unique_subject = no\n' > index.txt.attr
printf '01\n' > serial
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out /certs/ca.crt -days 3650 -subj '/CN=NetRunner test CA' >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -keyout /certs/server.key -out server.csr -subj '/CN=shop.lab' >/dev/null 2>&1
cat > ca.conf <<'CONF'
[ ca ]
default_ca = test
[ test ]
database = /tmp/ca/index.txt
new_certs_dir = /tmp/ca/newcerts
certificate = /certs/ca.crt
private_key = /tmp/ca/ca.key
serial = /tmp/ca/serial
default_md = sha256
policy = policy
x509_extensions = server
[ policy ]
commonName = supplied
[ server ]
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:shop.lab,DNS:news.lab
[ shop ]
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:shop.lab
[ news ]
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:news.lab
CONF
openssl ca -batch -config ca.conf -in server.csr -out /certs/valid.crt -days 3650 >/dev/null 2>&1
openssl ca -batch -config ca.conf -in server.csr -out /certs/expired.crt -startdate 20200101000000Z -enddate 20210101000000Z >/dev/null 2>&1
openssl ca -batch -config ca.conf -extensions shop -in server.csr -out /certs/shop.crt -days 3650 >/dev/null 2>&1
openssl ca -batch -config ca.conf -extensions news -in server.csr -out /certs/news.crt -days 3650 >/dev/null 2>&1
cp /certs/ca.crt /usr/local/share/ca-certificates/netrunner-test.crt
update-ca-certificates >/dev/null 2>&1
chmod 644 /certs/*
rm -rf /tmp/ca
