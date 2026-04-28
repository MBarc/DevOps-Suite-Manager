FROM guacamole/guacamole:1.5.5

# The guacamole image runs as a non-root user; switch to root for package ops.
USER root

# Download and install the auth-json extension at build time.
# The tar.gz contains guacamole-auth-json-1.5.5/guacamole-auth-json-1.5.5.jar.
RUN apt-get update && apt-get install -y --no-install-recommends curl tar \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /etc/guacamole/extensions \
    && curl -fsSL \
       https://downloads.apache.org/guacamole/1.5.5/binary/guacamole-auth-json-1.5.5.tar.gz \
       -o /tmp/auth-json.tar.gz \
    && tar -xzf /tmp/auth-json.tar.gz -C /tmp \
    && cp /tmp/guacamole-auth-json-1.5.5/guacamole-auth-json-1.5.5.jar \
          /etc/guacamole/extensions/ \
    && rm -rf /tmp/auth-json.tar.gz /tmp/guacamole-auth-json-1.5.5

COPY docker-entrypoint.sh /dosm-entrypoint.sh
RUN chmod +x /dosm-entrypoint.sh
ENTRYPOINT ["/dosm-entrypoint.sh"]
