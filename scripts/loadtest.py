#!/usr/bin/env python3
"""Calibrate MAX_CONCURRENT_REQUESTS against a real deployment.

The shipped ceiling is derived from the database connection budget, not
measured. This harness supplies the measurement: it drives a chosen concurrency
level at a public URL and reports the latency distribution plus how many
requests the server chose to shed (503) rather than queue.

Standard library only — no dependency to install on the server.

    python scripts/loadtest.py https://tawtheeq-ksa.com/ --concurrency 50 --requests 500

Read the result like this:
  * shed > 0                 the ceiling is engaging; that is the design working
  * p95 climbing with no shed and no errors → room to raise the ceiling
  * timeouts/5xx instead of shed → the ceiling is set too high for the hardware

Point it at a staging host, or at production only during an agreed window: it
generates real traffic.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter


def _one_request(url: str, timeout: float, headers: dict[str, str] | None = None) -> tuple[str, float]:
    started = time.perf_counter()
    request_headers = {"User-Agent": "tawtheeq-loadtest/1.0", **(headers or {})}
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            outcome = str(response.status)
    except urllib.error.HTTPError as exc:
        outcome = str(exc.code)
    except Exception as exc:
        outcome = type(exc).__name__
    return outcome, (time.perf_counter() - started) * 1000


def run(url: str, concurrency: int, total: int, timeout: float, headers: dict[str, str] | None = None) -> int:
    outcomes: Counter[str] = Counter()
    latencies: list[float] = []
    lock = threading.Lock()
    remaining = threading.Semaphore(0)
    counter = {"issued": 0}

    def worker() -> None:
        while True:
            with lock:
                if counter["issued"] >= total:
                    return
                counter["issued"] += 1
            outcome, elapsed = _one_request(url, timeout, headers)
            with lock:
                outcomes[outcome] += 1
                latencies.append(elapsed)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    wall_started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = time.perf_counter() - wall_started
    del remaining

    if not latencies:
        print("No responses recorded.")
        return 1

    latencies.sort()

    def pct(fraction: float) -> float:
        index = min(len(latencies) - 1, int(len(latencies) * fraction))
        return latencies[index]

    ok = sum(count for status, count in outcomes.items() if status.startswith("2"))
    shed = outcomes.get("503", 0)
    errors = sum(
        count
        for status, count in outcomes.items()
        if not status.startswith("2") and status != "503"
    )

    print(f"url          {url}")
    print(f"concurrency  {concurrency}")
    print(f"requests     {sum(outcomes.values())} in {wall:.1f}s "
          f"({sum(outcomes.values()) / wall:.1f} req/s)")
    print(f"served (2xx) {ok}")
    print(f"shed (503)   {shed}")
    print(f"errors       {errors}")
    print(f"latency ms   p50={statistics.median(latencies):.0f} "
          f"p95={pct(0.95):.0f} p99={pct(0.99):.0f} max={latencies[-1]:.0f}")
    print("outcomes     " + ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())))

    # A shed request is a deliberate, healthy response; a timeout or 5xx is not.
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="Public URL to exercise, e.g. https://host/")
    parser.add_argument("--concurrency", type=int, default=25, help="Simultaneous clients (default 25)")
    parser.add_argument("--requests", type=int, default=200, help="Total requests to issue (default 200)")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout in seconds")
    parser.add_argument("--host-header", help="Override Host when testing an origin directly")
    parser.add_argument(
        "--forwarded-proto",
        choices=("http", "https"),
        help="Send X-Forwarded-Proto (usually https behind Caddy/Cloudflare)",
    )
    args = parser.parse_args()

    if args.concurrency < 1 or args.requests < 1:
        parser.error("concurrency and requests must both be at least 1")

    headers = {}
    if args.host_header:
        headers["Host"] = args.host_header
    if args.forwarded_proto:
        headers["X-Forwarded-Proto"] = args.forwarded_proto
    return run(args.url, args.concurrency, args.requests, args.timeout, headers)


if __name__ == "__main__":
    sys.exit(main())
