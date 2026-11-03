"""Update checkout custody extends to an actual uv dependency writer."""
import shutil
import socket
import subprocess
import sys

import pytest

from hermes_cli.update_lock import UpdateLock


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("mode", ["bootstrap-uv", "pm-worker"])
def test_uv_writer_keeps_checkout_locked_after_bootstrap_dies(tmp_path, mode):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv executable unavailable")
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname="custody-test"\nversion="0.1.0"\nrequires-python=">=3.10"\ndependencies=["custody-fixture-unique-package==1.0"]\n')
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(30)
    program = """
import os, sys
from pathlib import Path
from hermes_cli.update_lock import UpdateLock
from pm.environment import PythonEnvironment
root = Path(sys.argv[1])
lock = UpdateLock(install_root=root)
assert lock.acquire()
env = dict(os.environ, UV_DEFAULT_INDEX=sys.argv[3], UV_HTTP_TIMEOUT='120')
os.environ.update(env)
def build():
    PythonEnvironment(uv=Path(sys.argv[2]), python=Path(sys.executable), destination=root/'venv', cache=root/'cache', env=env)._run(['lock'], cwd=root, timeout=120)
if sys.argv[4] == 'bootstrap-uv':
    build()
else:
    from pm import client
    source = Path(client.__file__).resolve().parents[1]
    worker = source/'pm/worker.py'
    injection = f'''import runpy, sys, os
from pathlib import Path
sys.path.insert(0, {str(source)!r})
from pm.environment import PythonEnvironment
import pm.operations
root = Path({str(root)!r})
def build_environment(**kwargs):
    return PythonEnvironment(uv=Path({sys.argv[2]!r}), python=Path(sys.executable), destination=root/'venv', cache=root/'cache', env=dict(os.environ))._run(['lock'], cwd=root, timeout=120).returncode
pm.operations.build_environment = build_environment
runpy.run_path({str(worker)!r}, run_name='__main__')
'''
    client._worker_command = lambda *args: [sys.executable, '-I', '-B', '-c', injection]
    client._request('build_environment', {})
"""
    parent = subprocess.Popen([sys.executable, "-c", program, str(root), uv,
                               f"http://127.0.0.1:{listener.getsockname()[1]}/simple/", mode],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    connection = None
    lock = UpdateLock(path=tmp_path / "contender", install_root=root)
    try:
        connection, _ = listener.accept()
        connection.recv(4096)  # An actual uv request is in flight.
        parent.kill()
        parent.wait(timeout=10)
        assert not lock.acquire(), "real uv writer survived bootstrap without checkout custody"
        connection.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        connection.close()
        connection = None
        from tests.waiting import wait_until
        wait_until(lock.acquire, timeout=30, what="uv writer custody to settle")
    finally:
        lock.release()
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        if connection is not None:
            connection.close()
        listener.close()
        parent.stdout.close()
        parent.stderr.close()
