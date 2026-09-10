"""Serve exactly one immutable, non-secret Terraform archive; no directory listing."""

import hashlib
import http.server
import os
from pathlib import Path


def handler(archive: Path, expected: str):
    content = archive.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("Module archive digest mismatch")

    class StaticModule(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != f"/{expected}.tar.gz":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            return

    return StaticModule


if __name__ == "__main__":
    sha = os.environ["MODULE_SHA256"]
    server = http.server.HTTPServer(
        ("0.0.0.0", 18080),
        handler(Path("/module/archive.tar.gz"), sha),
    )
    server.serve_forever()
