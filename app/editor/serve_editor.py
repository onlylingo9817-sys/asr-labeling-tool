#!/usr/bin/env python3
"""Static server with HTTP Range support (needed for audio seeking)."""
from __future__ import annotations

import argparse
import os
import re
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


class RangeHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_HEAD(self):
        self._serve(send_body=False)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/health":
            body = b'{"ok":true}'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._serve(send_body=True)

    def _serve(self, send_body: bool):
        path = Path(self.translate_path(self.path))
        if not path.is_file():
            return super().do_GET() if send_body else super().do_HEAD()
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        header = self.headers.get("Range")
        if header:
            match = RANGE_RE.fullmatch(header.strip())
            if match:
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                elif last:
                    start = max(0, size - int(last))
                else:
                    start = 0
                if start >= size or start > end:
                    self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                end = min(end, size - 1)
                status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not send_body:
            return
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # Audio players routinely abort a Range request while seeking.
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dir", default=str(Path(__file__).parent))
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args()
    os.chdir(args.dir)
    server = ThreadingHTTPServer((args.bind, args.port), RangeHandler)
    print(f"Serving {args.dir} at http://{args.bind}:{args.port}/ (Range enabled)")
    server.serve_forever()


if __name__ == "__main__":
    main()
