"""Stub OSRM server serving the synthetic Dallas -> New York fixture (benchmarks and smoke runs)."""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "dallas_ny_route.json"


def make_server(port: int, delay_ms: float = 0.0) -> ThreadingHTTPServer:
    fx = json.loads(FIXTURE.read_text())
    route = {
        "geometry": fx["polyline"],
        "distance": fx["distance_miles"] * 1609.344,
        "duration": fx["duration_s"],
    }
    body = json.dumps({"code": "Ok", "routes": [route]}).encode()
    state = {"calls": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/_stats":
                payload = json.dumps(state).encode()
            else:
                with lock:
                    state["calls"] += 1
                time.sleep(delay_ms / 1000)
                payload = body
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--delay-ms", type=float, default=0.0)
    args = ap.parse_args()
    make_server(args.port, args.delay_ms).serve_forever()
