"""Serve exactly one immutable, non-secret Terraform archive; no directory listing."""

import hashlib
import http.server
import os
import re
from pathlib import Path


def handler(archive: Path, expected: str, *, module_root: Path = Path("/module")):
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Module archive digest is invalid")
    root = module_root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Module archive directory is invalid")
    resolved = archive.resolve(strict=True)
    if (
        not archive.absolute().is_relative_to(root)
        or not resolved.is_relative_to(root.resolve(strict=True))
        or not resolved.is_file()
    ):
        raise ValueError("Module archive path is outside the module directory or not a file")
    content = resolved.read_bytes()
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


def serve(module_root: Path | None = None) -> None:
    module_root = module_root or Path(os.environ.get("MODULE_ROOT", "/module"))
    sha = os.environ["MODULE_SHA256"]
    server = http.server.HTTPServer(
        ("0.0.0.0", 18080),
        handler(module_root / "archive.tar.gz", sha, module_root=module_root),
    )
    server.serve_forever()


if __name__ == "__main__":
    serve()
