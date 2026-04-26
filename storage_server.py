#!/usr/bin/env python3

import argparse
import json
import mimetypes
import os
import posixpath
import shutil
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import unquote, urlsplit


def http_date_from_ts(ts: float) -> str:
    # RFC 7231 IMF-fixdate
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def safe_join(root: Path, url_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """
    Map URL path to filesystem path under root.
    Prevent path traversal; returns (path, error_message).
    """
    # strip query/fragment if passed accidentally
    raw_path = urlsplit(url_path).path
    # decode %xx and normalize to POSIX
    raw_path = unquote(raw_path)
    raw_path = posixpath.normpath(raw_path)

    # normpath collapses // and ..; ensure it remains absolute-like for split
    if raw_path.startswith("../") or raw_path == "..":
        return None, "Invalid path"

    # Remove leading slash to make it relative
    rel = raw_path.lstrip("/")
    # Disallow empty segments like "." after normalization
    if rel == ".":
        rel = ""

    candidate = (root / rel).resolve()
    try:
        root_resolved = root.resolve()
    except FileNotFoundError:
        root_resolved = root.absolute()

    if candidate == root_resolved or str(candidate).startswith(str(root_resolved) + os.sep):
        return candidate, None
    return None, "Path escapes storage root"


class StorageHandler(BaseHTTPRequestHandler):
    server_version = "Lab5Storage/1.0"

    @property
    def storage_root(self) -> Path:
        return self.server.storage_root  # type: ignore[attr-defined]

    def log_message(self, format: str, *args) -> None:
        # quieter: log basic info to stderr
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), format % args))

    def _send_json(self, status: int, payload: object) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, status: int, text: str) -> None:
        data = (text + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _resolve_target(self) -> Tuple[Optional[Path], Optional[str]]:
        p, err = safe_join(self.storage_root, self.path)
        return p, err

    def do_GET(self) -> None:
        target, err = self._resolve_target()
        if err or target is None:
            self._send_text(HTTPStatus.BAD_REQUEST, err or "Bad request")
            return

        if not target.exists():
            self._send_text(HTTPStatus.NOT_FOUND, "Not found")
            return

        if target.is_dir():
            # Directory listing as JSON
            try:
                items = []
                for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                    st = entry.stat()
                    items.append(
                        {
                            "name": entry.name,
                            "type": "dir" if entry.is_dir() else "file",
                            "size": st.st_size if entry.is_file() else None,
                            "mtime": int(st.st_mtime),
                        }
                    )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "path": "/" + str(target.relative_to(self.storage_root)).replace(os.sep, "/")
                        if target != self.storage_root
                        else "/",
                        "items": items,
                    },
                )
            except OSError as exc:
                self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, f"Failed to list directory: {exc}")
            return

        # File
        try:
            st = target.stat()
            ctype, _enc = mimetypes.guess_type(str(target))
            if not ctype:
                ctype = "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(st.st_size))
            self.send_header("Last-Modified", http_date_from_ts(st.st_mtime))
            self.end_headers()

            with target.open("rb") as f:
                shutil.copyfileobj(f, self.wfile, length=64 * 1024)
        except OSError as exc:
            self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, f"Failed to read file: {exc}")

    def do_HEAD(self) -> None:
        target, err = self._resolve_target()
        if err or target is None:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()
            return

        if not target.exists() or not target.is_file():
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return

        try:
            st = target.stat()
            ctype, _enc = mimetypes.guess_type(str(target))
            if not ctype:
                ctype = "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(st.st_size))
            self.send_header("Last-Modified", http_date_from_ts(st.st_mtime))
            self.end_headers()
        except OSError:
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self.end_headers()

    def do_PUT(self) -> None:
        target, err = self._resolve_target()
        if err or target is None:
            self._send_text(HTTPStatus.BAD_REQUEST, err or "Bad request")
            return

        # prevent using PUT on directory URLs (trailing slash)
        if self.path.endswith("/"):
            self._send_text(HTTPStatus.BAD_REQUEST, "PUT target must be a file path (no trailing slash)")
            return

        length_s = self.headers.get("Content-Length")
        if length_s is None:
            self._send_text(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
            return

        try:
            length = int(length_s)
        except ValueError:
            self._send_text(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return

        if length < 0:
            self._send_text(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, f"Failed to create directories: {exc}")
            return

        existed = target.exists()
        tmp = target.with_name(target.name + ".upload_tmp")
        try:
            with tmp.open("wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)

            if remaining != 0:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                self._send_text(HTTPStatus.BAD_REQUEST, "Client closed before sending full body")
                return

            tmp.replace(target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, f"Failed to store file: {exc}")
            return

        self.send_response(HTTPStatus.OK if existed else HTTPStatus.CREATED)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self) -> None:
        target, err = self._resolve_target()
        if err or target is None:
            self._send_text(HTTPStatus.BAD_REQUEST, err or "Bad request")
            return

        if not target.exists():
            self._send_text(HTTPStatus.NOT_FOUND, "Not found")
            return

        # Don't allow deleting the storage root itself
        if target.resolve() == self.storage_root.resolve():
            self._send_text(HTTPStatus.FORBIDDEN, "Refusing to delete storage root")
            return

        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, f"Delete failed: {exc}")
            return

        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.end_headers()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Lab 5: remote file storage over HTTP (REST API)")
    p.add_argument("--host", default="127.0.0.1", help="Listen host")
    p.add_argument("--port", type=int, default=8000, help="Listen port")
    p.add_argument(
        "--root",
        default="storage_data",
        help="Storage directory (will be created if missing)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)

    httpd = ThreadingHTTPServer((args.host, args.port), StorageHandler)
    httpd.storage_root = root  # type: ignore[attr-defined]

    print(f"Storage server listening on http://{args.host}:{args.port}/")
    print(f"Storage root: {root.resolve()}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

