"""Minimal logging.

Output format:
    14:23:51  hsw keys fetched          (3.4s)

No colors, no brackets, no decorations. Read like a Unix tool.
"""
import sys
import time as _time


class Logger:
    def __init__(self, prefix: str = "", stream=None):
        self.prefix = prefix
        self.stream = stream or sys.stderr
        self.start_time = _time.time()

    def info(self, message: str, start: float | None = None,
             end: float | None = None) -> None:
        self._write(message, start, end)

    def debug(self, message: str, start: float | None = None,
              end: float | None = None) -> None:
        self._write(message, start, end)

    def warn(self, message: str, start: float | None = None,
             end: float | None = None) -> None:
        self._write("warn: " + message, start, end)

    def error(self, message: str, start: float | None = None,
              end: float | None = None) -> None:
        self._write("error: " + message, start, end)

    def _write(self, message: str, start: float | None,
               end: float | None) -> None:
        ts = _time.strftime("%H:%M:%S", _time.localtime())
        elapsed = ""
        if start is not None and end is not None:
            dt = abs(end - start)  # callers sometimes pass these reversed
            if dt < 0.001:
                pass  # too short to mention
            elif dt < 1.0:
                elapsed = f"  ({dt*1000:.0f}ms)"
            else:
                elapsed = f"  ({dt:.1f}s)"
        prefix = f"{self.prefix}  " if self.prefix else ""
        line = f"{ts}  {prefix}{message}{elapsed}\n"
        self.stream.write(line)
        self.stream.flush()
