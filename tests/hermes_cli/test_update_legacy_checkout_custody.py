"""The completion bootstrap must fence checkout writes from legacy parents."""
import pytest

from hermes_cli import update_completion


@pytest.mark.platforms("posix")
def test_bootstrap_refuses_before_mutation_if_checkout_is_owned(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    marker = tmp_path / "marker"
    # A distinct process owns the lock, as a concurrent updater would.
    import subprocess
    import sys

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
