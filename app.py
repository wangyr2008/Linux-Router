from __future__ import annotations

from datetime import timedelta

from flask import Flask

from router_panel.core import ensure_auth_config, ensure_secret_key
from router_panel.web import register_routes


app = Flask(__name__)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

ensure_auth_config()
app.secret_key = ensure_secret_key()
register_routes(app)


if __name__ == "__main__":
    # Development only: production runs gunicorn through router-panel.service,
    # which adds the sandboxing (NoNewPrivileges, CapabilityBoundingSet, ...).
    # Binding 0.0.0.0:80 here would bypass all of it.
    app.run(host="127.0.0.1", port=8080)
