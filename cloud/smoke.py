#!/usr/bin/env python3
"""Live acceptance check for the Modal endpoint. Standard library only.

    export BONSAI_MODAL_URL=https://<workspace>--bonsai-cloud-serve.modal.run
    export MODAL_PROXY_TOKEN_ID=wk-...  MODAL_PROXY_TOKEN_SECRET=ws-...
    python3 cloud/smoke.py                      # health (cold start) + short chat
    python3 cloud/smoke.py --long 12000         # streamed generation past 150 s

Each run costs real GPU time: cold start + generation + the idle scaledown window.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def headers(env=None):
    env = os.environ if env is None else env
    key, secret = env.get("MODAL_PROXY_TOKEN_ID", ""), env.get("MODAL_PROXY_TOKEN_SECRET", "")
    if not (key.startswith("wk-") and secret.startswith("ws-")):
        raise SystemExit("Set MODAL_PROXY_TOKEN_ID (wk-...) and MODAL_PROXY_TOKEN_SECRET (ws-...)")
    return {"Modal-Key": key, "Modal-Secret": secret, "Content-Type": "application/json"}


def base_url(env=None):
    env = os.environ if env is None else env
    url = env.get("BONSAI_MODAL_URL", "").rstrip("/")
    if not url.startswith("https://") or not url.endswith(".modal.run"):
        raise SystemExit("Set BONSAI_MODAL_URL to the https://...modal.run URL printed by modal deploy")
    return url


def request(url, hdrs, body=None, timeout=900):
    data = None if body is None else json.dumps(body).encode()
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdrs), timeout=timeout)


def health(url, hdrs):
    start = time.monotonic()
    with request(f"{url}/health", hdrs) as reply:
        reply.read()
    return time.monotonic() - start


def chat(url, hdrs, prompt, max_tokens):
    body = {"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False}}
    with request(f"{url}/v1/chat/completions", hdrs, body) as reply:
        data = json.load(reply)
    timings = data.get("timings", {})
    return data["choices"][0]["message"]["content"], timings.get("predicted_per_second")


def stream(url, hdrs, tokens):
    """Generate exactly `tokens` tokens via SSE; reports where a cut happens."""
    body = {"messages": [{"role": "user", "content": "Count upward from 1, one number per line."}],
            "max_tokens": tokens, "ignore_eos": True, "stream": True,
            "chat_template_kwargs": {"enable_thinking": False}}
    start, chunks, status = time.monotonic(), 0, None
    try:
        with request(f"{url}/v1/chat/completions", hdrs, body, timeout=tokens) as reply:
            status = reply.status
            for line in reply:
                if line.startswith(b"data: ") and line.strip() != b"data: [DONE]":
                    chunks += 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"  stream interrupted after {time.monotonic() - start:.0f}s: {exc}")
    return status, chunks, time.monotonic() - start


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--long", type=int, metavar="TOKENS",
                        help="also stream TOKENS tokens (12000 ≈ 160 s on L40S) to test the 150 s limit")
    args = parser.parse_args()
    url, hdrs = base_url(), headers()
    try:
        print(f"Health (includes any cold start): {health(url, hdrs):.1f}s")
        start = time.monotonic()
        text, tps = chat(url, hdrs, "In one sentence: what is a ternary-weight language model?", 128)
        print(f"Chat: {time.monotonic() - start:.1f}s, {tps or 0:.1f} tok/s\n  {text.strip()[:300]}")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"HTTP {exc.code}: {exc.read()[:300]!r}") from None
    if args.long:
        status, chunks, seconds = stream(url, hdrs, args.long)
        verdict = "OK" if chunks >= args.long * 0.95 else "CUT SHORT"
        print(f"Stream: HTTP {status}, {chunks}/{args.long} chunks in {seconds:.0f}s -> {verdict}")
        if verdict != "OK":
            sys.exit(1)


if __name__ == "__main__":
    main()
