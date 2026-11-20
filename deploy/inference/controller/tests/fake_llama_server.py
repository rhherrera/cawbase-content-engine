#!/usr/bin/env python3
"""Fake `llama-server` for tests (standard library only). It emulates the parts of the llama.cpp server contract the
controller and the test executor use; it does NOT run a model. Output is a minimal object shaped by the requested
JSON Schema, which is enough to exercise the transport, readiness, slots, context limits and error mapping.

In-process: `FakeLlama(...).start()`. As a process it accepts the llama-server flags written by
deploy/inference/server/run-server.sh (unknown flags are ignored), plus FAKE_LLAMA_LOAD_S (seconds before /health is
200) and FAKE_LLAMA_GEN_S (seconds per completion) from the environment.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socketserver
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Responder = Callable[[dict[str, Any]], dict[str, Any]]


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:  # skip HTTPServer's reverse DNS lookup (slow on some hosts)
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)


def shaped(schema: dict[str, Any]) -> Any:
    t = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        props = schema.get("properties", {})
        return {k: shaped(props[k]) for k in schema.get("required", list(props))}
    if t == "array":
        return []
    if t == "boolean":
        return True
    if t in ("integer", "number"):
        return 0
    return "x"


class FakeLlama:
    def __init__(
        self,
        *,
        alias: str = "qwen3.6-27b-q4km",
        ctx_total: int = 8192,
        parallel: int = 1,
        api_key: str | None = None,
        load_s: float = 0.0,
        gen_s: float = 0.0,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.alias = alias
        self.ctx_total = ctx_total
        self.parallel = parallel
        self.api_key = api_key
        self.load_s = load_s
        self.gen_s = gen_s
        self.ready = threading.Event()
        self.hang = threading.Event()  # completions block while set
        self.responder: Responder | None = None
        self.slots = threading.BoundedSemaphore(parallel)
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.counts: dict[str, int] = {}
        self.tokens_predicted = 0
        self.requests: list[dict[str, Any]] = []
        self._server = _Server((host, port), self._handler())
        self._thread: threading.Thread | None = None
        self._timer: threading.Timer | None = None

    @property
    def n_ctx_slot(self) -> int:
        return self.ctx_total // self.parallel

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeLlama:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        if self.load_s <= 0:
            self.ready.set()
        else:
            self._timer = threading.Timer(self.load_s, self.ready.set)
            self._timer.daemon = True
            self._timer.start()
        return self

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self.ready.clear()
        self._server.shutdown()
        self._server.server_close()

    @staticmethod
    def tokens(text: str) -> int:
        return max(1, math.ceil(len(text) / 4))

    def _count(self, key: str) -> None:
        with self.lock:
            self.counts[key] = self.counts.get(key, 0) + 1

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: Any, ctype: str = "application/json") -> None:
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self) -> bool:
                if not fake.api_key:
                    return True
                return self.headers.get("Authorization") == f"Bearer {fake.api_key}"

            def _body(self) -> Any:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"null")

            def _loading(self) -> bool:
                if not fake.ready.is_set():
                    self._send(503, {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}})
                    return True
                return False

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                fake._count(f"GET {path}")
                if path in ("/health", "/v1/health"):
                    if not self._loading():
                        self._send(200, {"status": "ok"})
                    return
                if path in ("/v1/models", "/models"):
                    if not self._loading():
                        self._send(200, {"object": "list", "data": [{"id": fake.alias, "object": "model"}]})
                    return
                if not self._authorized():
                    self._send(
                        401, {"error": {"code": 401, "message": "Invalid API Key", "type": "authentication_error"}}
                    )
                    return
                if self._loading():
                    return
                if path == "/props":
                    self._send(
                        200, {"default_generation_settings": {"n_ctx": fake.n_ctx_slot}, "total_slots": fake.parallel}
                    )
                elif path == "/metrics":
                    text = (
                        f"llamacpp:tokens_predicted_total {fake.tokens_predicted}\n"
                        f"llamacpp:requests_processing {fake.in_flight}\nllamacpp:requests_deferred 0\n"
                    )
                    self._send(200, text, "text/plain; version=0.0.4")
                else:
                    self._send(404, {"error": {"code": 404, "message": "File Not Found", "type": "not_found_error"}})

            def do_POST(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                fake._count(f"POST {path}")
                if not self._authorized():
                    self._send(
                        401, {"error": {"code": 401, "message": "Invalid API Key", "type": "authentication_error"}}
                    )
                    return
                if self._loading():
                    return
                body = self._body()
                if path == "/apply-template":
                    prompt = "".join(f"<|{m['role']}|>{m['content']}" for m in body["messages"])
                    self._send(200, {"prompt": prompt})
                elif path == "/tokenize":
                    self._send(200, {"tokens": list(range(fake.tokens(body["content"])))})
                elif path in ("/v1/chat/completions", "/chat/completions"):
                    self._chat(body)
                else:
                    self._send(404, {"error": {"code": 404, "message": "File Not Found", "type": "not_found_error"}})

            def _chat(self, body: dict[str, Any]) -> None:
                prompt = "".join(f"<|{m['role']}|>{m['content']}" for m in body["messages"])
                n_prompt = fake.tokens(prompt)
                if n_prompt > fake.n_ctx_slot:
                    self._send(
                        400,
                        {
                            "error": {
                                "code": 400,
                                "type": "exceed_context_size_error",
                                "message": "the request exceeds the available context size",
                                "n_prompt_tokens": n_prompt,
                                "n_ctx": fake.n_ctx_slot,
                            }
                        },
                    )
                    return
                with fake.lock:
                    fake.requests.append(body)
                with fake.slots:  # requests beyond the slots wait here, like the server's deferred queue
                    with fake.lock:
                        fake.in_flight += 1
                        fake.max_in_flight = max(fake.max_in_flight, fake.in_flight)
                    try:
                        while fake.hang.is_set():
                            time.sleep(0.01)
                        if fake.gen_s:
                            time.sleep(fake.gen_s)
                        rf = body.get("response_format") or {}
                        schema = rf.get("json_schema", {}).get("schema", {"type": "object"})
                        output = fake.responder(body) if fake.responder else shaped(schema)
                    finally:
                        with fake.lock:
                            fake.in_flight -= 1
                n_gen = 7
                with fake.lock:
                    fake.tokens_predicted += n_gen
                self._send(
                    200,
                    {
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": json.dumps(output)},
                            }
                        ],
                        "usage": {
                            "prompt_tokens": n_prompt,
                            "completion_tokens": n_gen,
                            "total_tokens": n_prompt + n_gen,
                        },
                        "timings": {"prompt_n": n_prompt, "predicted_n": n_gen, "predicted_per_second": 50.0},
                    },
                )

        return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--model", required=True)
    ap.add_argument("-a", "--alias", default="model")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("-c", "--ctx-size", type=int, default=8192)
    ap.add_argument("-np", "--parallel", type=int, default=1)
    ap.add_argument("--api-key-file")
    args, _unknown = ap.parse_known_args(argv)
    if not os.path.exists(args.model):
        print("model file not found", file=sys.stderr)
        return 1
    key = None
    if args.api_key_file:
        key = next(line.strip() for line in open(args.api_key_file, encoding="utf-8") if line.strip())
    fake = FakeLlama(
        alias=args.alias,
        ctx_total=args.ctx_size,
        parallel=args.parallel,
        api_key=key,
        load_s=float(os.environ.get("FAKE_LLAMA_LOAD_S", "0")),
        gen_s=float(os.environ.get("FAKE_LLAMA_GEN_S", "0")),
        host=args.host,
        port=args.port,
    ).start()
    print(f"fake llama-server listening on {fake.base_url}", flush=True)
    stop = threading.Event()
    import signal

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    fake.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
