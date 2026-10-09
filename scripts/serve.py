"""Multi-worker server launcher.

`uvicorn --workers N` binds its shared listener without an explicit TCP protocol, so asyncio skips
TCP_NODELAY on accepted connections, so keep-alive responses stall ~40 ms (Nagle + delayed ACK).
Binding the listener with IPPROTO_TCP here avoids that. N=1 (default) just runs plain uvicorn.
"""

import multiprocessing
import os
import signal
import socket
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from any cwd, no install

APP = "config.asgi:application"


def _worker(sock: socket.socket) -> None:
    uvicorn.Server(uvicorn.Config(APP, log_level="info", access_log=False)).run(sockets=[sock])


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
    if workers <= 1 or "fork" not in multiprocessing.get_all_start_methods():
        uvicorn.run(APP, host=host, port=port, access_log=False)
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(2048)
    sock.set_inheritable(True)
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_worker, args=(sock,)) for _ in range(workers)]
    for p in procs:
        p.start()

    def stop(*_):
        for p in procs:
            p.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for p in procs:
        p.join()
    sys.exit(0)


if __name__ == "__main__":
    main()
