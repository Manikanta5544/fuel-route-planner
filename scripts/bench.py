"""Latency benchmark: starts the stub OSRM and uvicorn, then runs four scenarios (ARCHITECTURE 12).

The routing provider is a STUB serving a synthetic route, so cold numbers exclude real network
latency except for the optional --stub-delay-ms.
"""

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.stub_osrm import make_server  # noqa: E402


def pct(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q / 100 * len(ordered)))]


async def run(client, bodies, concurrency):
    sem, results = asyncio.Semaphore(concurrency), []

    async def one(body):
        async with sem:
            t = time.perf_counter()
            try:
                r = await client.post("/api/v1/route", json=body)
                ok, meta = (
                    r.status_code == 200,
                    (r.json().get("meta") if r.status_code == 200 else {}),
                )
            except httpx.HTTPError:
                ok, meta = False, {}
            results.append((ok, (time.perf_counter() - t) * 1e3, meta))

    start = time.perf_counter()
    await asyncio.gather(*(one(b) for b in bodies))
    return results, time.perf_counter() - start


def summarize(name, results, elapsed, stub_calls):
    lat = [ms for ok, ms, _ in results if ok]
    if not lat:
        raise SystemExit(f"{name}: every request failed")
    metas = [m for ok, _, m in results if ok]
    hits = sum(m["cache"]["route"] in ("hit", "coalesced") for m in metas)
    calls = sum(m["routing"]["routing_calls"] for m in metas)
    row = {
        "scenario": name,
        "requests": len(results),
        "p50_ms": round(pct(lat, 50), 1),
        "p95_ms": round(pct(lat, 95), 1),
        "p99_ms": round(pct(lat, 99), 1),
        "rps": round(len(results) / elapsed, 1),
        "error_rate": round(1 - len(lat) / len(results), 4),
        "routing_calls_per_request": round(calls / len(results), 3),
        "stub_upstream_calls": stub_calls,
        "cache_hit_ratio": round(hits / len(metas), 3),
    }
    print(json.dumps(row))
    return row


def body(i):
    """Distinct, valid coordinates inside the USA (the stub returns the same route)."""
    return {
        "start": {"lat": 32.7767 + i * 0.0001, "lng": -96.797},
        "finish": {"lat": 40.7128 + i * 0.0001, "lng": -74.006},
    }


async def scenarios(args, stub_calls):
    rows = []
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{args.port}",
        timeout=30,
        limits=httpx.Limits(max_connections=200),
    ) as c:
        for _ in range(100):
            try:
                if (await c.get("/readyz")).status_code == 200:
                    break
            except httpx.HTTPError:
                await asyncio.sleep(0.2)
        n = args.requests
        before = stub_calls()
        await run(c, [body(0)], 1)  # prime: one cold request
        results, elapsed = await run(c, [body(0)] * n, args.concurrency)
        rows.append(summarize("warm", results, elapsed, stub_calls() - before - 1))
        before = stub_calls()
        results, elapsed = await run(c, [body(10 + i) for i in range(n)], 1)
        rows.append(summarize("cold", results, elapsed, stub_calls() - before))
        before = stub_calls()
        results, elapsed = await run(c, [body(5000)] * 100, 100)
        rows.append(summarize("identical-concurrent(100)", results, elapsed, stub_calls() - before))
        before = stub_calls()
        results, elapsed = await run(c, [body(6000 + i) for i in range(100)], 100)
        rows.append(summarize("distinct-concurrent(100)", results, elapsed, stub_calls() - before))
    return rows


def main(args):
    stub = make_server(args.stub_port, args.stub_delay_ms)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    env = {
        **os.environ,
        "OSRM_BASE_URL": f"http://127.0.0.1:{args.stub_port}",
        "ORS_API_KEY": "",
        "REDIS_URL": "",
        "RATE_LIMIT_PER_MIN": "0",
        "ALLOWED_HOSTS": "127.0.0.1,localhost",
    }
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "config.asgi:application",
            "--port",
            str(args.port),
            "--workers",
            str(args.workers),
            "--no-access-log",
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
    )

    def stub_calls():
        return httpx.get(f"http://127.0.0.1:{args.stub_port}/_stats").json()["calls"]

    try:
        rows = asyncio.run(scenarios(args, stub_calls))
    finally:
        server.terminate()
        server.wait()
        stub.shutdown()
    env_info = {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "workers": args.workers,
        "stub_delay_ms": args.stub_delay_ms,
        "provider": "STUB (synthetic route fixture)",
    }
    print(json.dumps(env_info))
    (ROOT / "bench-results.json").write_text(json.dumps({"env": env_info, "rows": rows}, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--stub-port", type=int, default=9100)
    ap.add_argument("--stub-delay-ms", type=float, default=0.0)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--requests", type=int, default=300)
    ap.add_argument("--concurrency", type=int, default=20)
    main(ap.parse_args())
