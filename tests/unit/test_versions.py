import pytest

from drivecon.versions import Member, describe, folders_similar

DAY = 86400.0


def m(name: str, size: int, mtime: float, drive: str = "A") -> Member:
    return Member("h" + name, f"Files/{name}", size, mtime, (drive,))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("a", "b", "similar"),
    [
        (("finance",), ("finance",), True),  # same folder
        (("finance",), ("finance", "taxes"), True),  # one inside the other
        (("finance", "taxes", "2011"), ("finance",), True),
        ((), ("finance",), True),  # the drive root and a folder under it
        (("finance", "taxes", "2011"), ("finance", "taxes", "2012"), False),  # different years
        (("a",), ("b",), False),
        (("work", "taxes"), ("old-pc", "taxes"), True),  # same folder name, other parents
        (("docs", "plan"), ("docs", "plan-old"), True),  # near-identical sibling names
        (("a", "b", "c", "d"), ("a", "b", "c", "e"), False),
        (("projects", "alpha"), ("music", "rock", "deep"), False),
        (("projects",), ("music",), False),
    ],
)
def test_folders_similar(a: tuple[str, ...], b: tuple[str, ...], similar: bool) -> None:
    assert folders_similar(a, b) is similar


@pytest.mark.unit
def test_describe_suggests_the_newer_file_by_date() -> None:
    notes = describe([m("new/b.xlsx", 60, 5 * DAY, "USB"), m("b.xlsx", 50, 1 * DAY, "Dell")])
    assert len(notes) == 1 and "may be a newer version" in notes[0]
    assert "Files/new/b.xlsx [USB]" in notes[0] and "Files/b.xlsx [Dell]" in notes[0]
    assert "1970-01-06" in notes[0] and "1970-01-02" in notes[0]


@pytest.mark.unit
def test_describe_does_not_claim_newer_when_dates_agree_but_sizes_differ() -> None:
    notes = describe([m("a.doc", 60, DAY + 1), m("b/a.doc", 50, DAY)])
    assert "cannot tell which is newer" in notes[0] and "newer version" not in notes[0]


@pytest.mark.unit
def test_describe_flags_same_size_and_date_with_different_content() -> None:
    notes = describe([m("a.doc", 50, DAY), m("b/a.doc", 50, DAY + 1)])
    assert "possible corruption" in notes[0]
