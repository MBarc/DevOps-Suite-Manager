# Guacamole Postgres init scripts

Place the Postgres schema dumped from the Guacamole image here so the database
is initialized on first start. Generate it with:

    docker run --rm guacamole/guacamole:1.5.5 /opt/guacamole/bin/initdb.sh --postgres > 001-initdb.sql

The `docker-entrypoint-initdb.d` mount in `docker-compose.yml` will run any
`*.sql` files in this directory the first time the postgres container comes
up. Subsequent runs are skipped.

Auth-json connections do not need any schema rows — the extension hands
Guacamole an entire short-lived connection blob each time. The Postgres DB
exists only because the Guacamole webapp insists on a primary auth source,
and it's where you'd later add a local admin account if you wanted UI-side
management.
