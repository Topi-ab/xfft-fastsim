"""A small S3 server on 127.0.0.1 for the cache's tests: objects by path (path-style addressing, any bucket, any
credentials, no signature check), GET, HEAD, PUT, DELETE and ListObjectsV2. It can hold requests longer than the
client's read timeout, to stand in for an overloaded server: botocore then raises ReadTimeoutError through its
real HTTP stack, with response None, as an overloaded server makes it do.

Every request is answered after delay_s (a slow server; inflight and peak count the requests it is working
on). Holding: request number i (counted over all requests) is held when hold_every > 0 and i % hold_every == 0; the
object of a held request is held for its next hold_repeat - 1 requests as well (hold_repeat = 3 outlasts the
cache's three attempts: the lookup fails and the case runs). hold_first = n holds the first n requests of every
object (n = 1: each request times out once, the retry answers).

    python tests/unit/fake_s3.py --port 9000 --hold-every 7 --hold-repeat 3 --hold-s 31
    SIM_CACHE_URL=s3://results/fft AWS_ENDPOINT_URL=http://127.0.0.1:9000 pytest ...
"""

from __future__ import annotations

import argparse
import hashlib
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape


class FakeS3Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 0, hold_s: float = 0.0, hold_every: int = 0, hold_repeat: int = 1,
                 hold_first: int = 0, delay_s: float = 0.0, verbose: bool = False) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.objects: dict[str, bytes] = {}
        self.lock = threading.Lock()
        self.hold_s, self.hold_every, self.hold_repeat, self.hold_first = hold_s, hold_every, hold_repeat, hold_first
        self.delay_s = delay_s
        self.verbose = verbose
        self.inflight = self.peak = 0
        self.requests = 0
        self.held = 0
        self.seen: dict[str, int] = {}
        self.pending: dict[str, int] = {}          # object -> requests still to hold
        self.log: list[tuple[str, str, bool]] = []  # (method, path, held)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> FakeS3Server:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()

    def hold(self, method: str, path: str) -> bool:
        """Whether this request is held (counted here)."""
        with self.lock:
            self.requests += 1
            n = self.seen.get(path, 0)
            self.seen[path] = n + 1
            held = n < self.hold_first
            if self.pending.get(path, 0) > 0:
                self.pending[path] -= 1
                held = True
            elif self.hold_every and self.requests % self.hold_every == 0:
                self.pending[path] = self.hold_repeat - 1
                held = True
            self.held += held
            self.log.append((method, path, held))
            return held


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"      # keep-alive and Expect: 100-continue, as boto3 uses them
    server: FakeS3Server

    def log_message(self, fmt, *args) -> None:
        if self.server.verbose:
            super().log_message(fmt, *args)

    def path_and_query(self) -> tuple[str, dict]:
        parts = urlsplit(self.path)
        return unquote(parts.path), parse_qs(parts.query)

    def body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n) if n else b""
        if "aws-chunked" in (self.headers.get("Content-Encoding") or ""):
            data = dechunk(data)
        return data

    def answer(self, status: int, data: bytes = b"", headers: dict | None = None, head: bool = False) -> None:
        self.send_response(status)
        for k, v in {"Content-Length": str(len(data)), **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if not head and data:
            self.wfile.write(data)

    def error(self, status: int, code: str, head: bool = False) -> None:
        xml = f"<?xml version='1.0' encoding='UTF-8'?><Error><Code>{code}</Code><Message>{code}</Message></Error>"
        self.answer(status, xml.encode(), {"Content-Type": "application/xml"}, head)

    def held(self) -> bool:
        """Hold the request past the client's read timeout; True when it was held (the client has given up)."""
        s = self.server
        with s.lock:
            s.inflight += 1
            s.peak = max(s.peak, s.inflight)
        try:
            time.sleep(s.delay_s)
            path, _ = self.path_and_query()
            if not s.hold(self.command, path):
                return False
            time.sleep(s.hold_s)
            self.close_connection = True
            return True
        finally:
            with s.lock:
                s.inflight -= 1

    def do_GET(self) -> None:
        self.get(head=False)

    def do_HEAD(self) -> None:
        self.get(head=True)

    def get(self, head: bool) -> None:
        if self.held():
            return
        path, query = self.path_and_query()
        if "list-type" in query:
            self.list(path, query)
            return
        data = self.server.objects.get(path)
        if data is None:
            self.error(404, "NoSuchKey", head)
            return
        self.answer(200, data, {"ETag": f'"{hashlib.md5(data).hexdigest()}"', "Content-Type": "binary/octet-stream",
                                "Last-Modified": formatdate(usegmt=True)}, head)

    def do_PUT(self) -> None:
        data = self.body()
        if self.held():
            return
        path, _ = self.path_and_query()
        self.server.objects[path] = data
        self.answer(200, headers={"ETag": f'"{hashlib.md5(data).hexdigest()}"'})

    def do_DELETE(self) -> None:
        if self.held():
            return
        path, _ = self.path_and_query()
        self.server.objects.pop(path, None)
        self.answer(204)

    def list(self, path: str, query: dict) -> None:
        bucket = path.strip("/")
        prefix = f"/{bucket}/" + query.get("prefix", [""])[0]
        keys = sorted(k for k in list(self.server.objects) if k.startswith(prefix))
        items = "".join(f"<Contents><Key>{escape(k[len(bucket) + 2:])}</Key><Size>{len(self.server.objects[k])}"
                        f"</Size></Contents>" for k in keys)
        xml = (f"<?xml version='1.0' encoding='UTF-8'?><ListBucketResult><Name>{escape(bucket)}</Name>"
               f"<KeyCount>{len(keys)}</KeyCount><IsTruncated>false</IsTruncated>{items}</ListBucketResult>")
        self.answer(200, xml.encode(), {"Content-Type": "application/xml"})


def dechunk(data: bytes) -> bytes:
    """The payload of an aws-chunked body (chunks of "<hex size>[;...]\\r\\n<data>\\r\\n", then trailers)."""
    out, i = b"", 0
    while i < len(data):
        end = data.index(b"\r\n", i)
        n = int(data[i:end].split(b";")[0], 16)
        if n == 0:
            break
        out += data[end + 2:end + 2 + n]
        i = end + 2 + n + 2
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--hold-s", type=float, default=31.0, help="how long a held request is held (s)")
    parser.add_argument("--hold-every", type=int, default=0, help="hold every n-th request (0: none)")
    parser.add_argument("--hold-repeat", type=int, default=1, help="requests of a held object held in all")
    parser.add_argument("--hold-first", type=int, default=0, help="hold the first n requests of every object")
    parser.add_argument("--delay-s", type=float, default=0.0, help="answer every request after this (s)")
    parser.add_argument("--verbose", action="store_true")
    a = parser.parse_args()
    server = FakeS3Server(a.port, a.hold_s, a.hold_every, a.hold_repeat, a.hold_first, a.delay_s, a.verbose)
    print(f"fake S3 at {server.url}", flush=True)
    last = 0.0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(5)
            if time.monotonic() - last >= 60:
                last = time.monotonic()
                print(f"{time.strftime('%H:%M:%S')} {server.requests} requests, {server.held} held, "
                      f"{len(server.objects)} objects", flush=True)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
