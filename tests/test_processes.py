"""Real process transport regressions; no external crypto tools required."""
import repowatch.errors as errors
import repowatch.verification.gpg as verification_gpg
from pathlib import Path
import subprocess
import sys
import time

import pytest

from repowatch.processes import ProcessStream, OutputLimitError, run


def command(code):
    return [sys.executable, '-c', code]


def test_nonzero_exit_and_literal_arguments():
    result = run(command('import sys; print(sys.argv[1]); print("err", file=sys.stderr); sys.exit(7)')
                 + ['$(exit 99); literal'], timeout=5, text=True)
    assert result.returncode == 7
    assert result.stdout == '$(exit 99); literal\n'
    assert result.stderr == 'err\n'


def test_large_input_and_both_output_pipes():
    data = b'x' * (1024 * 1024)
    result = run(command('import sys; sys.stderr.buffer.write(b"e"*300000); '
                         'sys.stderr.flush(); sys.stdout.buffer.write(sys.stdin.buffer.read())'),
                 timeout=5, input=data)
    assert result.stdout == data
    assert result.stderr == b'e' * 300000


@pytest.mark.parametrize('pipe', ['stdout', 'stderr'])
def test_output_limits_stop_producer(pipe):
    with pytest.raises(OutputLimitError, match=pipe):
        run(command(f'import sys; sys.{pipe}.buffer.write(b"x"*1000000)'),
            timeout=5, max_stdout=100, max_stderr=100)


def test_timeout_covers_blocked_read_and_reaps_process():
    stream = ProcessStream(command('import time; time.sleep(20)'), timeout=0.1)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        stream.read(1)
    assert time.monotonic() - started < 3
    assert stream._process.poll() is not None
    assert stream._process.stdout.closed and stream._process.stderr.closed


def test_timeout_after_output_pipes_close():
    with pytest.raises(subprocess.TimeoutExpired):
        run(command('import os,time; os.close(1); os.close(2); time.sleep(20)'), timeout=0.1)


def test_parser_exception_aborts_process_and_is_preserved():
    with pytest.raises(RuntimeError, match='parser failed'):
        with ProcessStream(command('import time; print("ready", flush=True); time.sleep(20)'), timeout=5) as stream:
            assert stream.read(6) == b'ready\n'
            raise RuntimeError('parser failed')
    assert stream._process.poll() is not None
    stream.close()


def test_streaming_short_reads_and_final_exit():
    with ProcessStream(command('import sys; sys.stdout.buffer.write(b"x"*200000); sys.exit(9)'), timeout=5) as stream:
        chunks = []
        while chunk := stream.read(1024):
            assert len(chunk) <= 1024
            chunks.append(chunk)
    assert b''.join(chunks) == b'x' * 200000
    assert stream.returncode == 9


def test_timeout_stops_descendant(tmp_path):
    marker = tmp_path / 'child-survived'
    child = 'import time; from pathlib import Path; time.sleep(0.7); Path(' + repr(str(marker)) + ').touch()'
    parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); time.sleep(20)'
    with pytest.raises(subprocess.TimeoutExpired):
        run(command(parent), timeout=0.2)
    time.sleep(0.8)
    assert not marker.exists()


def test_missing_binary():
    with pytest.raises(FileNotFoundError):
        run(['/nonexistent/repowatch-tool'], timeout=5)


def test_gpg_timeout_maps_to_signature_error(monkeypatch):
    import repowatch.verification.gpg as gpgverify
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(['gpgv'], 30)
    monkeypatch.setattr(verification_gpg, 'run', fail)
    with pytest.raises(errors.SignatureError, match='process failed'):
        verification_gpg._run_gpgv('keyring', Path('signature'), None)
