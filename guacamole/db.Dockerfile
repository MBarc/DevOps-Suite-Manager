FROM guacamole/guacamole:1.5.5 AS sql-gen

FROM postgres:16-alpine
COPY --from=sql-gen /opt/guacamole/bin/initdb.sh /tmp/initdb.sh
RUN sh /tmp/initdb.sh --postgresql > /docker-entrypoint-initdb.d/001-initdb.sql \
    && rm /tmp/initdb.sh
