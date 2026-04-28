#!/bin/sh
set -e

# guacamole's start.sh generates /etc/guacamole/guacamole.properties from
# env vars (Postgres, guacd, etc.) and then calls `exec catalina.sh run` to
# start Tomcat.  The auth-json extension reads json-secret-key and
# json-trusted-networks from that same file, but start.sh doesn't know about
# extension-specific settings.
#
# Fix: put a shim catalina.sh first in PATH.  When start.sh calls it, we
# append the auth-json properties, then hand off to the real catalina.sh.

SHIM=$(mktemp -d)
cat > "$SHIM/catalina.sh" << 'INNER'
#!/bin/sh
PROPS=/etc/guacamole/guacamole.properties
if [ -n "$JSON_SECRET_KEY" ] && ! grep -q "^json-secret-key" "$PROPS" 2>/dev/null; then
    printf "\njson-secret-key: %s\n" "$JSON_SECRET_KEY" >> "$PROPS"
fi
if [ -n "$JSON_TRUSTED_NETWORKS" ] && ! grep -q "^json-trusted-networks" "$PROPS" 2>/dev/null; then
    printf "json-trusted-networks: %s\n" "$JSON_TRUSTED_NETWORKS" >> "$PROPS"
fi
exec /usr/local/tomcat/bin/catalina.sh "$@"
INNER
chmod +x "$SHIM/catalina.sh"

export PATH="$SHIM:$PATH"
exec /opt/guacamole/bin/start.sh
