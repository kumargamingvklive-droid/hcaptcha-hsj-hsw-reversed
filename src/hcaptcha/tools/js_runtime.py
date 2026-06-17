"""Self-contained Node + jsdom sandbox bridge.

Boots a child Node process running `_js_runner.js`, then exposes a
clean Python interface for eval'ing JavaScript inside a real jsdom
realm with canvas + WebAssembly + crypto polyfilled.

This is the only entry point for any code in this repository that
needs to execute JS — `keyfetcher_hsj`, `keyfetcher_hsw`, and the
WASM bytecode-patch fetcher all go through here.

Usage:

    rt = JsRuntime()
    rt.eval(hsj_source)                         # load a bundle
    rt.eval("globalThis.__r = hsj(siteKey)")    # run it
    val = rt.eval("globalThis.__r")             # read back
    rt.close()
"""
import json
import os
import shutil
import subprocess
import threading
import time
from typing import Any


_RUNNER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "_js_runner.js",
)


class JsRuntime:
    def __init__(self, ready_timeout: float = 30.0):
        # Fail fast with an actionable message if the toolchain is missing,
        # instead of a cryptic "process terminated" later on.
        if shutil.which("node") is None:
            raise RuntimeError(
                "Node.js ('node') was not found on PATH. The hsj/hsw "
                "extractors need Node 18+ with jsdom + canvas installed "
                "(`npm install` in the repo root). Install Node and retry."
            )
        node_modules = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))), "node_modules")
        if not os.path.isdir(os.path.join(node_modules, "jsdom")):
            raise RuntimeError(
                "Node dependency 'jsdom' is missing. Run `npm install` in "
                "the repository root before using the live extractors."
            )
        try:
            self._proc = subprocess.Popen(
                ["node", _RUNNER_PATH],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except (FileNotFoundError, OSError) as e:
            raise RuntimeError(f"failed to launch Node sandbox: {e}") from e
        self._lock = threading.Lock()
        self._next_id = 1
        self._closed = False

        # Wait for ready handshake
        deadline = time.time() + ready_timeout
        while True:
            line = self._proc.stdout.readline()
            if not line:
                stderr = self._proc.stderr.read().decode("utf-8", "replace")
                raise RuntimeError(
                    f"JS runner failed to start: {stderr.strip()}"
                )
            if line.strip() == b"__READY__":
                break
            if time.time() > deadline:
                raise TimeoutError("JS runner did not signal ready")

    # -----------------------------------------------------------------
    # public API
    # -----------------------------------------------------------------
    def eval(self, code: str, *,
             suppress: bool = False,
             byte_array: bool = False,
             timeout: float = 120.0) -> Any:
        """Evaluate `code` in the realm. Returns the deserialized value.

        - `suppress=True`     : swallow JS exceptions, return None
        - `byte_array=True`   : interpret result as a list of byte values
                                 and return as Python bytes
        - `timeout`           : wall-clock seconds before TimeoutError
        """
        if self._closed:
            raise RuntimeError("runtime is closed")
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            payload = json.dumps({"id": req_id, "code": code},
                                 ensure_ascii=False) + "\n"
            self._proc.stdin.write(payload.encode("utf-8"))
            self._proc.stdin.flush()

            deadline = time.time() + timeout
            while True:
                if time.time() > deadline:
                    raise TimeoutError(f"JS eval timed out after {timeout}s")
                raw = self._proc.stdout.readline()
                if not raw:
                    stderr = self._proc.stderr.read().decode("utf-8", "replace")
                    raise RuntimeError(
                        f"JS runner died: {stderr.strip()}"
                    )
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if msg.get("id") != req_id:
                    continue
                if not msg.get("ok"):
                    if suppress:
                        return None
                    raise RuntimeError(f"JS error: {msg.get('error')}")
                result = msg.get("result")
                if byte_array:
                    if result is None:
                        return b""
                    if not isinstance(result, list):
                        raise TypeError(
                            f"expected byte array, got {type(result).__name__}"
                        )
                    # Empty or flat list of ints -> bytes
                    if not result or isinstance(result[0], int):
                        return bytes(result)
                    # Nested (list of byte arrays) -> return as-is so the
                    # caller can index in and convert each element.
                    return result
                return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
