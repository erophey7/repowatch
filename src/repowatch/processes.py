"""Synchronous POSIX process transport, independent of tool exit-code policy.

Call from asyncio.to_thread in asynchronous paths. One deadline covers pipe
reads and process exit. Both output pipes are drained together; failures kill
and reap the process group. Callers interpret return codes and tool output.
"""
from __future__ import annotations

import os
import io
import selectors
import signal
import subprocess
import tempfile
import time
from contextlib import ExitStack
from collections.abc import Mapping, Sequence


class OutputLimitError(subprocess.SubprocessError):
    """A subprocess exceeded its configured output budget."""


class ProcessStream:
    """Context-managed binary stdout stream with bounded stderr capture.

    read(size) may return a short read. Streaming stdout has no total limit
    unless supplied; buffered stderr always has a limit. Input bytes use a
    temporary file, avoiding a feeder thread and stdin/stdout pipe deadlocks.
    No shell, extra process, global queue, or tool-specific environment policy.
    """

    def __init__(self, args: Sequence[str], *, timeout: float,
                 input: bytes | None = None, env: Mapping[str, str] | None = None,
                 max_stdout: int | None = None, max_stderr: int = 1024 * 1024):
        if isinstance(args, (str, bytes)) or not args:
            raise ValueError('args must be a nonempty argument sequence')
        if timeout <= 0 or max_stderr < 0 or (max_stdout is not None and max_stdout < 0):
            raise ValueError('invalid process timeout or output limit')
        self.args = list(args)
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.max_stdout = max_stdout
        self.max_stderr = max_stderr
        self.stderr = bytearray()
        self.returncode = None
        self._stdout_size = 0
        self._closed = False
        self._resources = ExitStack()
        self._process = None
        try:
            source = subprocess.DEVNULL
            if input is not None:
                source = self._resources.enter_context(tempfile.TemporaryFile())
                source.write(input)
                source.seek(0)
            # Two pipes do not need epoll's extra descriptor and registration
            # syscalls. Match subprocess's poll/select choice for small sets.
            selector = getattr(selectors, 'PollSelector', selectors.SelectSelector)
            self._selector = self._resources.enter_context(selector())
            self._process = subprocess.Popen(self.args, stdin=source, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, env=env, start_new_session=True)
            for pipe, kind in ((self._process.stdout, 'stdout'), (self._process.stderr, 'stderr')):
                self._resources.enter_context(pipe)
                self._selector.register(pipe, selectors.EVENT_READ, kind)
        except BaseException:
            self.abort()
            raise

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(self.args, self.timeout)
        return remaining

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError('read of closed process stream')
        if size == 0:
            return b''
        try:
            if size < 0:
                # Avoid retaining every block plus a second full-size buffer
                # during join. BytesIO can hand its buffer to getvalue().
                output = io.BytesIO()
                while chunk := self.read(65536):
                    output.write(chunk)
                return output.getvalue()
            while self._selector.get_map():
                for key, _ in self._selector.select(self._remaining()):
                    chunk = os.read(key.fd, min(size, 65536) if key.data == 'stdout' else 65536)
                    if not chunk:
                        self._selector.unregister(key.fileobj)
                    elif key.data == 'stderr':
                        if len(self.stderr) + len(chunk) > self.max_stderr:
                            raise OutputLimitError('process stderr limit exceeded')
                        self.stderr.extend(chunk)
                    else:
                        self._stdout_size += len(chunk)
                        if self.max_stdout is not None and self._stdout_size > self.max_stdout:
                            raise OutputLimitError('process stdout limit exceeded')
                        return chunk
            if self.returncode is None:
                self.returncode = self._process.wait(timeout=self._remaining())
            return b''
        except BaseException:
            self.abort()
            raise

    def abort(self) -> None:
        """Terminate this invocation and descendants, then release descriptors."""
        if self._closed:
            return
        try:
            if self._process is not None:
                try:
                    os.killpg(self._process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.returncode = self._process.wait()
        finally:
            self._closed = True
            self._resources.close()

    def close(self) -> None:
        if self._closed:
            return
        try:
            while self.read(65536):
                pass
        finally:
            self._closed = True
            self._resources.close()

    def __enter__(self) -> ProcessStream:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None:
            self.abort()
        else:
            self.close()


def run(args: Sequence[str], *, timeout: float, input: bytes | None = None,
        env: Mapping[str, str] | None = None, text: bool = False,
        max_stdout: int = 128 * 1024 * 1024,
        max_stderr: int = 1024 * 1024) -> subprocess.CompletedProcess:
    """Capture bounded output; nonzero exits are returned for caller policy."""
    with ProcessStream(args, timeout=timeout, input=input, env=env,
                       max_stdout=max_stdout, max_stderr=max_stderr) as stream:
        stdout = stream.read()
    stderr = bytes(stream.stderr)
    if text:
        stdout = stdout.decode('utf-8', errors='replace')
        stderr = stderr.decode('utf-8', errors='replace')
    return subprocess.CompletedProcess(list(args), stream.returncode, stdout, stderr)
