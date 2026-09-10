"""Serve an isolated database for browser regressions; never start the user's instance."""
import json
import os
import sys
from pathlib import Path

if not os.environ.get("LEYLINEBOOK_DATA_DIR") or not os.environ.get("LEYLINEBOOK_SESSION_TOKEN"):
    raise RuntimeError("Browser tests require an isolated data directory and session")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

app.initialize_database()
server, port = app.create_http_server(0)
print("TEST_SERVER=" + json.dumps({"port": port}), flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
