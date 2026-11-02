"""The completion bootstrap must fence checkout writes from legacy parents."""
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import update_completion, update_lock
from tests.waiting import wait_until


@pytest.mark.platforms("posix")
def test_bootstrap_refuses_before_mutation_if_checkout_is_owned(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    marker = tmp_path / "marker"
    # A distinct process owns the lock, as a concurrent updater would.
    owner = subprocess.Popen(
        [sys.executable, "-c", "import sys; from pathlib import Path; "
         "from hermes_cli.update_lock import UpdateLock; "
         "l=UpdateLock(path=Path(sys.argv[2]), install_root=Path(sys.argv[1])); "
         "assert l.acquire(); print('ready', flush=True); sys.stdin.read()", str(root), str(marker)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert owner.stdout.readline().strip() == "ready"
        from hermes_cli import venv_sync
        mutated = []
        monkeypatch.setattr(venv_sync, "arm_completion", lambda _: mutated.append(True))
        with pytest.raises(RuntimeError, match="checkout lock"):
            update_completion._prepare({"source": str(root), "receipt": {"update_id": "test"}},
                                       tmp_path / "request", tmp_path / "result")
        assert mutated == []
    finally:
        owner.stdin.close()
        owner.wait(timeout=10)
        owner.stdout.close()


@pytest.mark.platforms("posix")
def test_current_parent_bootstrap_joins_inherited_lock(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    lock = update_lock.UpdateLock(path=tmp_path / "marker", install_root=root)
    assert lock.acquire()
    program = """
import sys
from pathlib import Path
from hermes_cli import update_completion, venv_sync
class ReachedArm(Exception): pass
def arm(_): raise ReachedArm()
venv_sync.arm_completion = arm
try:
    update_completion._prepare({'source': sys.argv[1], 'receipt': {'update_id': 'test'}}, Path(sys.argv[2]), Path(sys.argv[3]))
except ReachedArm:
    print('joined')
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", program, str(root), str(tmp_path / "request"), str(tmp_path / "result")],
            pass_fds=update_lock.checkout_lock_fds(root), text=True, capture_output=True, timeout=10, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "joined"
    finally:
        lock.release()


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass  # Only the fixture's detached completion group is signalled.
def test_legacy_parent_death_leaves_bootstrap_in_checkout_custody(tmp_path):
    # Frozen original transport executes the actual new bootstrap in a disposable checkout.
    # PM's network-dependent operation is replaced by a real, deliberately paused child.
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "checkout"
    cli = root / "hermes_cli"
    pm = root / "pm"
    cli.mkdir(parents=True)
    pm.mkdir()
    for name in ("__init__.py", "update_completion.py", "update_lock.py"):
        shutil.copyfile(source / "hermes_cli" / name, cli / name)
    (cli / "venv_sync.py").write_text("def arm_completion(root): pass\ndef collect_superseded_generations(root): pass\n")
    (cli / "gitlock.py").write_text("def convert_treeless_checkout_first(root): pass\n")
    (pm / "__init__.py").write_text("")
    (pm / "receipt.py").write_text("from contextlib import nullcontext\ndef worker_context(_): return nullcontext()\n")
    (pm / "environments.py").write_text("def activation_environment(root): return {}\ndef project_python(root): return None\n")
    ready = tmp_path / "pm-ready"
    (pm / "client.py").write_text(
        "import os, pathlib, subprocess, sys\n"
        "def ensure_tools_for_sync():\n"
        " p=subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)\n"
        f" pathlib.Path({str(ready)!r}).write_text(str(os.getpid())+' '+str(p.pid))\n"
        " p.wait()\n"
    )
    parent_code = """
import sys
sys.path.insert(0, sys.argv[1])
from tests.compat.legacy_update_completion import run_completion
run_completion({'schema': 1, 'source': sys.argv[2], 'home': sys.argv[3], 'receipt': {'update_id': 'test'}})
"""
    parent = subprocess.Popen([sys.executable, "-c", parent_code, str(source), str(root), str(tmp_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    bootstrap = None
    contender = update_lock.UpdateLock(path=tmp_path / "contender", install_root=root)
    try:
        wait_until(lambda: ready.exists() or parent.poll() is not None, timeout=20, what="legacy completion's PM child")
        if not ready.exists():
            raise AssertionError(f"legacy transport exited early: {parent.communicate(timeout=5)}")
        bootstrap, writer = map(int, ready.read_text().split())
        parent.terminate()
        assert parent.wait(timeout=10) == -signal.SIGTERM
        os.kill(bootstrap, 0)
        os.kill(writer, 0)
        assert not contender.acquire()
        os.killpg(bootstrap, signal.SIGKILL)  # windows-footgun: ok -- retained fixture session
        bootstrap = None
        wait_until(contender.acquire, timeout=20, what="checkout custody to be released")
    finally:
        contender.release()
        if bootstrap is not None:
            try:
                os.killpg(bootstrap, signal.SIGKILL)  # windows-footgun: ok -- retained fixture session
            except ProcessLookupError:
                pass
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
