FROM guacamole/guacamole:1.5.5

# The auth-json JAR is placed here by scripts/setup-guacamole.sh before the
# first `docker-compose up --build`.
COPY extensions/ /etc/guacamole/extensions/

# Custom entrypoint: guacamole's start.sh generates guacamole.properties from
# env vars and then exec's catalina.sh to start Tomcat.  We shim catalina.sh
# so we can append the auth-json settings right after properties are written
# but before Tomcat accepts connections.
COPY docker-entrypoint.sh /dosm-entrypoint.sh
RUN chmod +x /dosm-entrypoint.sh

ENTRYPOINT ["/dosm-entrypoint.sh"]
