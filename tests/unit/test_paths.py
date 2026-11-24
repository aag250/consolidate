import datetime as dt

import pytest
from hypothesis import given
from hypothesis import strategies as st

from drivecon.config import PhotoRules
from drivecon.paths import (
    FORBIDDEN,
    RESERVED,
    Candidate,
    best_candidate,
    conflict_name,
    is_camera_dump,
    limit_length,
    path_length,
    sanitize_segment,
    split_ext,
    strip_prefix,
)

PREFIXES = [
    "Users/*/Documents/",
    "Users/*/",
    "Documents and Settings/*/My Documents/",
    "home/*/",
    "Backup [0-9][0-9][0-9][0-9]/",
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Users/joe/Documents/finance/taxes/2011/w2.pdf", "finance/taxes/2011/w2.pdf"),
        ("Backup 2012/finance/taxes/2011/w2.pdf", "finance/taxes/2011/w2.pdf"),
        ("C:/Users/joe/Documents/a.docx", "a.docx"),
        ("Documents and Settings/joe/My Documents/a.docx", "a.docx"),
        ("home/ann/notes/a.txt", "notes/a.txt"),
        ("Users/joe/Documents/Backup 2012/a/b.txt", "a/b.txt"),  # wrapper inside a profile
        ("Users/joe/Desktop/a.docx", "Desktop/a.docx"),
        ("Users/joe/a.docx", "a.docx"),  # the file name is never stripped
        ("Users/a.docx", "Users/a.docx"),
        ("USERS/Joe/DOCUMENTS/a.docx", "a.docx"),  # case-insensitive
        ("Backups of stuff/a.docx", "Backups of stuff/a.docx"),
    ],
)
def test_strip_prefix(path: str, expected: str) -> None:
    assert "/".join(strip_prefix(path.split("/"), PREFIXES)) == expected


@pytest.mark.unit
def test_doc_example_two_drives_map_to_the_same_place() -> None:
    a = strip_prefix(["Users", "joe", "Documents", "finance", "taxes", "2011", "w2.pdf"], PREFIXES)
    b = strip_prefix(["Backup 2012", "finance", "taxes", "2011", "w2.pdf"], PREFIXES)
    assert a == b


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a:b.txt", "a_b.txt"),
        ("what?.doc", "what_.doc"),
        ('q"<>|*\\x', "q______x"),
        ("name. . ", "name"),
        ("trailing.", "trailing"),
        ("CON.txt", "_CON.txt"),
        ("con", "_con"),
        ("Lpt3.tar.gz", "_Lpt3.tar.gz"),
        ("COM1 .txt", "_COM1 .txt"),
        ("CONSOLE.txt", "CONSOLE.txt"),
        ("a\x01b\x1f", "a_b_"),
        ("...", "_"),
        ("", "_"),
    ],
)
def test_sanitize_segment(name: str, expected: str) -> None:
    assert sanitize_segment(name) == expected


@pytest.mark.unit
@given(st.text(max_size=400))
def test_sanitize_always_yields_a_legal_idempotent_name(name: str) -> None:
    out = sanitize_segment(name)
    assert out and not (set(out) & FORBIDDEN) and all(ord(c) >= 32 for c in out)
    assert out == out.rstrip(" .")
    assert out.split(".")[0].rstrip(" ").upper() not in RESERVED
    assert len(out.encode("utf-16-le")) // 2 <= 255
    assert sanitize_segment(out) == out


@pytest.mark.unit
def test_limit_length_leaves_short_paths_alone() -> None:
    segs = ["Files", "finance", "w2.pdf"]
    assert limit_length(segs) == (segs, True)


@pytest.mark.unit
def test_limit_length_shortens_deepest_folders_first_and_keeps_the_filename() -> None:
    segs = ["Files", "a" * 60, "b" * 60, "c" * 60, "d" * 60, "report.docx"]
    out, fits = limit_length(segs)
    assert fits and path_length(out) < 240
    assert out[0] == "Files" and out[-1] == "report.docx" and out[1] == segs[1]
    assert len(out[4]) < 60 and "~" in out[4]


@pytest.mark.unit
def test_shortened_siblings_stay_distinct() -> None:
    a, _ = limit_length(["Files", "x" * 120 + "A", "y" * 120 + "A", "f.txt"])
    b, _ = limit_length(["Files", "x" * 120 + "A", "y" * 120 + "B", "f.txt"])
    assert a != b


@pytest.mark.unit
@given(st.lists(st.text("abcdef ghij.", min_size=1, max_size=80), min_size=1, max_size=12))
def test_limit_length_invariants(parts: list[str]) -> None:
    segs = ["Files", *[p.strip(" .") or "x" for p in parts]]
    out, fits = limit_length(segs)
    assert len(out) == len(segs) and out[0] == "Files"
    if fits:
        assert path_length(out) < 240
    if path_length(segs) < 240:
        assert out == segs
    assert split_ext(out[-1])[1] == split_ext(segs[-1])[1] or not fits or path_length(segs) < 240


def cand(sid: int, path: str, mtime: float = 100.0) -> Candidate:
    return Candidate(sid, f"S{sid}", tuple(path.split("/")), mtime)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cands", "winner"),
    [
        # a path seen on more than one drive beats a better-looking single path
        (
            [cand(1, "a/b/c/x.doc"), cand(2, "Desktop/x.doc"), cand(3, "Desktop/x.doc")],
            "Desktop/x.doc",
        ),
        # deeper named folder beats Desktop/ , Downloads/ and the drive root
        (
            [
                cand(1, "Desktop/x.doc"),
                cand(2, "Downloads/x.doc"),
                cand(3, "x.doc"),
                cand(4, "finance/taxes/x.doc"),
            ],
            "finance/taxes/x.doc",
        ),
        ([cand(1, "finance/x.doc"), cand(2, "finance/taxes/x.doc")], "finance/taxes/x.doc"),
        # then no backup/copy/old/(1) marker in the name
        ([cand(1, "work/x - Copy.doc"), cand(2, "work/x.doc")], "work/x.doc"),
        ([cand(1, "work/x (1).doc"), cand(2, "work/x.doc")], "work/x.doc"),
        ([cand(1, "work/x_old.doc"), cand(2, "work/x.doc")], "work/x.doc"),
        ([cand(1, "work/Gold.doc"), cand(2, "work/Gold.doc", 5.0)], "work/Gold.doc"),
        # then the oldest modified time
        ([cand(1, "work/a/x.doc", 50.0), cand(2, "work/b/x.doc", 10.0)], "work/b/x.doc"),
    ],
)
def test_best_candidate(cands: list[Candidate], winner: str) -> None:
    assert "/".join(best_candidate(cands).logical) == winner


@pytest.mark.unit
def test_marker_word_must_stand_alone() -> None:
    # "Gold" contains "old", "Scopy" contains "copy": neither is a marker
    c = [cand(1, "d/Gold.doc", 9.0), cand(2, "d/Gold2.doc", 1.0)]
    assert best_candidate(c).logical[-1] == "Gold2.doc"  # mtime decides, no marker on either


PHOTOS = PhotoRules(("jpg", "cr2"), ("DCIM", "Camera Roll"), r"^\d{3}[A-Za-z0-9_]{5}$")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "sd", "expected"),
    [
        ("DCIM/100CANON/IMG_1.jpg", False, True),
        ("dcim/x/IMG_1.jpg", False, True),
        ("100MEDIA/IMG_1.jpg", False, True),
        ("Camera Roll/IMG_1.jpg", False, True),
        ("IMG_1.jpg", True, True),  # root of an SD card
        ("IMG_1.jpg", False, False),
        ("Vacation/IMG_1.jpg", False, False),  # named folder stays where it was
        ("DCIM/notes.txt", False, False),  # not an image
        ("2015/summer.jpg", False, False),
    ],
)
def test_is_camera_dump(path: str, sd: bool, expected: bool) -> None:
    segs = path.split("/")
    assert is_camera_dump(segs, PHOTOS, segs[-1].rpartition(".")[2], sd) is expected


@pytest.mark.unit
def test_conflict_name_adds_source_and_date_and_numbers_repeats() -> None:
    mtime = dt.datetime(2011, 3, 4, 12, tzinfo=dt.UTC).timestamp()
    taken = {"files/finance/budget.xlsx"}
    first = conflict_name(
        "budget.xlsx", "Dell-2009-C", mtime, lambda p: p.lower() in taken, "Files/finance"
    )
    assert first == "budget (Dell-2009-C, 2011-03-04).xlsx"
    taken.add(f"files/finance/{first}".lower())
    second = conflict_name(
        "budget.xlsx", "Dell-2009-C", mtime, lambda p: p.lower() in taken, "Files/finance"
    )
    assert second == "budget (Dell-2009-C, 2011-03-04) 2.xlsx"


@pytest.mark.unit
def test_a_folder_holding_three_profile_folders_is_a_profile_whatever_its_name() -> None:
    from drivecon.paths import profile_roots

    dirs = [
        ("KG 10.23.21 Backup", "Desktop"),
        ("KG 10.23.21 Backup", "documents", "taxes"),
        ("KG 10.23.21 Backup", "Downloads"),
        ("Projects", "site", "Documents"),  # two profile names only: not a profile
        ("Projects", "site", "Pictures"),
    ]
    assert profile_roots(dirs) == [("KG 10.23.21 Backup",)]


@pytest.mark.unit
def test_profiles_are_listed_deepest_first_and_the_drive_root_can_be_one() -> None:
    from drivecon.paths import profile_roots

    dirs: list[tuple[str, ...]] = [("Desktop",), ("Documents",), ("Music",)]
    dirs += [("Documents", "old pc", "joe", d) for d in ("Desktop", "Documents", "AppData")]
    assert profile_roots(dirs) == [("Documents", "old pc", "joe"), ()]


@pytest.mark.unit
def test_profile_prefixes_merge_documents_and_escape_glob_characters() -> None:
    from drivecon.paths import profile_prefixes

    roots = [("Documents", "Old [PC]", "joe"), ()]
    prefixes = profile_prefixes(roots)
    assert prefixes == [
        "Documents/Old [[]PC]/joe/Documents/",
        "Documents/Old [[]PC]/joe/My Documents/",
        "Documents/Old [[]PC]/joe/",
        "Documents/",
        "My Documents/",
    ]

    def strip(p: str) -> str:
        return "/".join(strip_prefix(p.split("/"), prefixes))

    assert strip("Documents/Old [PC]/joe/Documents/taxes/w2.pdf") == "taxes/w2.pdf"
    assert strip("Documents/Old [PC]/joe/Desktop/a.txt") == "Desktop/a.txt"
    assert strip("Documents/budget.xlsx") == "budget.xlsx"
    assert strip("Desktop/b.txt") == "Desktop/b.txt"
