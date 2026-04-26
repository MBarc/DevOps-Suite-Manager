"""Apache Guacamole integration: signed JSON connection envelopes + iframe.

DOSM owns hosts and credentials; Guacamole is a dumb renderer. We sign a
short-lived JSON blob describing one connection (host, protocol, credentials
pulled from the secrets backend) with the auth-json shared secret, exchange
it for a Guacamole session token, and iframe the resulting client URL.
"""
from dosm.guacamole.auth_json import AuthJsonCodec, build_connection_id, load_secret_key
from dosm.guacamole.routes import router as guacamole_router

__all__ = ["AuthJsonCodec", "build_connection_id", "guacamole_router", "load_secret_key"]
