"""Read-only metadata endpoints for generation-focused local HTTP fixture servers."""
from __future__ import annotations

import http.server
import json


class OllamaMetadataHandler(http.server.BaseHTTPRequestHandler):
    metadata_models = ("model",)

    def _send_metadata(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send_metadata({"models": [
                {"name": name, "digest": "synthetic-legacy-digest"} for name in self.metadata_models
            ]})
        else:
            self.send_error(501, "Unsupported fixture GET")

    def serve_model_metadata(self):
        if self.path != "/api/show":
            return False
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if body.get("model") not in self.metadata_models:
            self._send_metadata({"error": "unknown fixture model"}, 404)
        else:
            # No weights exist in this fixture: retain an explicit unknown-mode policy.
            self._send_metadata({"model_info": {"general.architecture": "synthetic-legacy"}})
        return True
