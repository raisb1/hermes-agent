"""A refused completion must not be settled without checkout custody."""
from tests.hermes_cli.test_update_completion_process import transition, _run_cmd_update


def test_custody_refusal_does_not_settle_lost_completion(transition, monkeypatch, capsys):
    from hermes_cli import update_completion

    _root, _git, _old, _new, request = transition
    monkeypatch.setattr(update_completion, "run_completion", lambda _: {
        "exit_code": 1, "receipt": None, "custody_refused": True,
    })

    def forbidden(*args):
        raise AssertionError("settlement without custody")

    monkeypatch.setattr(update_completion, "settle_lost_completion", forbidden)
    code, _receipt, _out = _run_cmd_update(monkeypatch, request, capsys)
    assert code == 1
