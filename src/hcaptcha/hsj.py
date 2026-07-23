"""hsj.js key extractor.

hsj.js (a.k.a. "inspekt_client") is hCaptcha's fingerprinting/payload bundle.
It is asm.js / WASM-style compiled JS — i.e. it manipulates a JS Uint8Array
buffer with integer math instead of using real WebAssembly.

Both encryption keys used by hsj — the payload key (n_key) and the
response-decrypt key — are passed through the AES-256 key schedule routine
inside hsj.js. That routine always allocates a 480-byte stack frame
(`ve = n = ve - 480 | 0`), and the 32-byte input key sits at offset 0 of
that frame, in the `f` Int8Array view onto the wasm-style memory buffer.

We patch the routine to push a copy of those 32 bytes into a JS array
(`dumped_keys`), call hsj twice with sentinel inputs, and read the keys out.

Public API:
    HSJKeyFetcher(version=None).fetch_keys() -> dict
"""
import time
import requests
import jsbeautifier
from .tools.js_runtime import JsRuntime
from .log import Logger

from . import version as _v


class HSJKeyFetcher:
    def __init__(self, version: str | None = None, log: Logger | None = None):
        self.log = log or Logger()
        self.version = version or _v.latest_version()
        self.start_time = time.time()
        self.runtime = JsRuntime()
        r = requests.get(_v.asset_url(self.version, "hsj.js"))
        r.encoding = "utf-8"
        self.hsj_src = r.text
        self._inject_key_dump()

    def _inject_key_dump(self) -> None:
        start = time.time()
        src = "let dumped_keys = [];\n" + self.hsj_src
        lines = jsbeautifier.beautify(src).split("\n")

        for i in range(len(lines) - 10):
            if "- 480 |" not in lines[i]:
                continue
            try:
                key_var = lines[i].split("480), ")[1].split(", ")[0]
            except IndexError:
                continue
            self.log.debug(f"key var -> {key_var}", start=start, end=time.time())
            if "] = ~" not in lines[i + 2]:
                continue
            memory = lines[i + 2].split("] = ~")[1].split("[")[0]
            self.log.debug(f"memory buffer -> {memory}", start=start, end=time.time())
            anchor = lines[i].split(",")[0]
            lines[i] = lines[i].replace(
                anchor,
                f"{anchor}, dumped_keys.push(Array.from("
                f"new Uint8Array({memory}.buffer.slice({key_var}, {key_var} + 32))))",
            )
            self.log.debug("injected key dump", start=start, end=time.time())

        self.runtime.eval("\n".join(lines))

    def fetch_keys(self) -> dict:
        ops = [
            ("n_key", "hsj('IiI=.eyJzIjowLCJmIjowLCJjIjowfQ==.')"),
            ("response_decrypt_key", "hsj(0, new Uint8Array(1024))"),
            ("payload_encrypt_key", "hsj(1, new Uint8Array(1024))"),
        ]
        
        keys = {"version": self.version}

for expected_index, (name, code) in enumerate(ops):
    started = time.time()
    self.runtime.eval(code, suppress=True)
    
    dumped = self.runtime.eval("dumped_keys", byte_array=True)
    
    if not isinstance(dumped, list):
        raise RuntimeError(
            f"HSJ capture returned {type(dumped).__name__}, expected list"
        )
        self.log.info(
            f"{name}: capture count={len(dumped)}, "
            f"expected index={expected_index}",
            start=started,
            end=time.time(),
        )
        
        if expected_index >= len(dumped):
            lengths = [
                len(item) if hasattr(item, "__len__") else None
                for item in dumped
            ]
            
            raise RuntimeError(
                f"HSJ capture missing for {name}: "
                f"expected dumped_keys[{expected_index}], "
                f"but only {len(dumped)} capture(s) exist; "
                f"capture lengths={lengths}. "
                "The current bundle structure or invoked entry-point "
                "behaviour has changed."
            )
            
            raw = dumped[expected_index]
            
            if len(raw) != 32:
                raise RuntimeError(
                    f"Invalid captured length for {name}: "
                    f"expected 32 bytes, got {len(raw)}"
                )
                
                key = bytes(raw).hex()
                self.log.info(
                    f"{name.replace('_', ' ').title()} captured",
                    start=started,
                    end=time.time(),
                )
                keys[name] = key
                
                self.log.info(
                    "hsj keys fetched",
                    start=self.start_time,
                    end=time.time(),
                )
                return keys


if __name__ == "__main__":
    print(HSJKeyFetcher().fetch_keys())
