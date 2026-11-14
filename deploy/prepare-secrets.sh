#!/bin/sh
set -eu
# Trusted one-shot initializer; applications never mount the host secret directory.
install -d -m 0700 -o 10001 -g 10001 /runtime /migration /https
install -d -m 0700 -o 999 -g 999 /postgres
for target in /runtime /migration; do
    for file in session-key mfa-key ca.crt; do
        install -m 0400 -o 10001 -g 10001 "/source/$file" "$target/$file"
    done
done
install -m 0400 -o 10001 -g 10001 /source/runtime-password /runtime/db-password
install -m 0400 -o 10001 -g 10001 /source/migration-password /migration/db-password
install -m 0400 -o 10001 -g 10001 /source/bootstrap-password /migration/bootstrap-password
for file in https.crt https.key; do
    install -m 0400 -o 10001 -g 10001 "/source/$file" "/https/server.${file#*.}"
done
for file in postgres.crt postgres.key ca.crt admin-password runtime-password migration-password; do
    install -m 0400 -o 999 -g 999 "/source/$file" "/postgres/$file"
done
for file in pg_hba.conf init-database.sh; do
    install -m 0400 -o 999 -g 999 "/source-config/$file" "/postgres/$file"
done
