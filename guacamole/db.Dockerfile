# Generate the Guacamole schema in the Guacamole image (where initdb.sh and its
# schema/*.sql live), then ship only the generated SQL into Postgres. Running
# initdb.sh in the postgres stage fails - it cats /opt/guacamole/postgresql/
# schema/*.sql, which isn't present there.
FROM guacamole/guacamole:1.5.5 AS sql-gen
RUN /opt/guacamole/bin/initdb.sh --postgresql > /tmp/001-initdb.sql

FROM postgres:16-alpine
COPY --from=sql-gen /tmp/001-initdb.sql /docker-entrypoint-initdb.d/001-initdb.sql
