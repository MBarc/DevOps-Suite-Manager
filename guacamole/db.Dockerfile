FROM postgres:16-alpine
COPY initdb/001-initdb.sql /docker-entrypoint-initdb.d/001-initdb.sql
