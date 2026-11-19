import pytest

from drivecon import fault


@pytest.mark.unit
def test_fault_fires_on_nth_hit_only(monkeypatch: pytest.MonkeyPatch) -> None:
    fault.reset()
    monkeypatch.setenv("CONSOLIDATE_FAULT", "x.y:2:raise")
    fault.checkpoint("x.y")
    fault.checkpoint("other")
    with pytest.raises(fault.FaultInjected):
        fault.checkpoint("x.y")
    fault.checkpoint("x.y")  # third hit does nothing


@pytest.mark.unit
def test_fault_is_noop_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    fault.reset()
    monkeypatch.delenv("CONSOLIDATE_FAULT", raising=False)
    fault.checkpoint("anything")
