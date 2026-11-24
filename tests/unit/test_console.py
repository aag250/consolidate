from typing import Any

import pytest

from drivecon.console import Console, Meter, duration


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(**kw: Any) -> tuple[Console, list[str], Clock]:
    out: list[str] = []
    clock = Clock()
    return Console(out=out.append, clock=clock, heartbeat=30, remind_every=60, **kw), out, clock


@pytest.mark.unit
def test_meter_shows_percent_and_time_left_from_this_runs_rate() -> None:
    m = Meter(total=1000, final_total=True)
    m.update(200, 0.0)  # 200 already done by an earlier run: not part of the rate
    m.update(400, 100.0)  # 200 bytes in 100 s -> 600 left at 2/s
    assert m.summary(100.0) == "~40%, ~5m00s left"


@pytest.mark.unit
def test_meter_says_so_far_while_the_total_can_still_grow_and_never_claims_100() -> None:
    m = Meter(total=1000)
    m.update(999, 0.0)
    assert m.summary(50.0) == "~99% of 1.0 KB found so far"
    assert Meter().summary(0.0) == ""


@pytest.mark.unit
def test_status_is_held_while_a_question_is_open_and_printed_after() -> None:
    con, out, _ = make()
    con.status("scan", "10 files")

    def answer(prompt: str) -> str:
        con.status("scan", "20 files")
        con.status("hash", "5 files, 1 KB")
        assert out == ["[scan] 10 files", "Q?"]  # nothing printed under the question
        return "e"

    assert con.ask(["Q?"], "[e]xclude", read=answer) == "e"
    assert out[2:] == ["[hash] 5 files, 1 KB", "[scan] 20 files"]


@pytest.mark.unit
def test_open_question_is_repeated_and_says_when_only_the_answer_is_left() -> None:
    con, out, clock = make()
    con.expect("scan", "hash")

    def answer(prompt: str) -> str:
        clock.t += 61
        con.tick()
        assert out[-2:] == [
            "\n(Still waiting for your answer after 1m01s: work continues in the background.)",
            "Unknown extension '.lbr'",
        ]
        con.finished("scan")
        con.finished("hash")
        clock.t += 61
        con.tick()
        assert "hashing and scanning finished; the run is waiting only for this answer" in out[-2]
        return "e"

    con.ask(["Unknown extension '.lbr'"], "[e]xclude", read=answer)


@pytest.mark.unit
def test_heartbeat_names_the_file_a_phase_is_on_when_nothing_else_was_printed() -> None:
    con, out, clock = make()
    con.status("hash", "100 files, 1 GB")
    con.working_on("hash", "Videos/wedding.mov", 8_000_000_000)
    clock.t += 10
    con.tick()
    assert out == ["[hash] 100 files, 1 GB"]  # not yet quiet for a heartbeat
    clock.t += 25
    con.tick()
    assert out[-1] == "[working] hash on Videos/wedding.mov (8.0 GB) for 35s"


@pytest.mark.unit
def test_heartbeat_skips_finished_phases() -> None:
    con, out, clock = make()
    con.working_on("scan", "Users/")
    con.finished("scan")
    clock.t += 31
    con.tick()
    assert out == []


@pytest.mark.unit
def test_duration() -> None:
    assert (duration(5), duration(125), duration(7300)) == ("5s", "2m05s", "2h01m")
