#!/bin/sh
set -eu
runtime_password=$(cat /run/postgres/runtime-password)
migration_password=$(cat /run/postgres/migration-password)
# Locally generated URL-safe secrets only; passwords go through SQL stdin, never argv/logs.
case "$runtime_password$migration_password" in *[!A-Za-z0-9_-]*|'') exit 1;; esac
{
    printf "CREATE ROLE tagihnet_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '%s';\n" "$runtime_password"
    printf "CREATE ROLE tagihnet_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '%s';\n" "$migration_password"
    printf '%s\n' 'CREATE DATABASE tagihnet_staging OWNER tagihnet_migrator;'
} | psql --username "${POSTGRES_USER:-postgres}" --dbname postgres --set ON_ERROR_STOP=1 --quiet
psql --username "${POSTGRES_USER:-postgres}" --dbname tagihnet_staging --set ON_ERROR_STOP=1 --quiet <<'SQL'
REVOKE ALL ON DATABASE tagihnet_staging FROM PUBLIC;
GRANT CONNECT ON DATABASE tagihnet_staging TO tagihnet_runtime;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO tagihnet_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE tagihnet_migrator IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO tagihnet_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE tagihnet_migrator IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO tagihnet_runtime;
SQL
