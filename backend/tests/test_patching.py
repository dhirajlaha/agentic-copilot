import pytest

from app.agents import patching
from app.agents.patching import MAX_CHANGED_FILES, MAX_CHANGED_LINES, apply_edits, build_patch
from app.agents.schemas import CoderOutput, EditOut, FileChangeOut
from app.core.exceptions import RepositoryAccessError

BASE = "c" * 40
LOGIN = "def parse(email):\n    return email\n\n\ndef other():\n    return 1\n"


def files(**contents):
    """A read_file over {path: text} where __ in a keyword stands for / and _dot_ for ."""
    store = {k.replace("__", "/").replace("_dot_", "."): v for k, v in contents.items()}
    return lambda path: store.get(path)


def modify(path="src/login.py", *edits, reason="r"):
    return FileChangeOut(path=path, action="modify", edits=list(edits), content="", reason=reason)


def edit(search, replace):
    return EditOut(search=search, replace=replace)


def output(*changes, summary=" Does a thing. ", questions=()):
    return CoderOutput(summary=summary, changes=list(changes), open_questions=list(questions))


def patch_for(out, read_file=None, attempts=1):
    return build_patch(
        out,
        base_commit_sha=BASE,
        read_file=read_file or files(src__login_dot_py=LOGIN),
        attempts=attempts,
    )


# ---- paths --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["src/app.py", "README.md", "a/b/c/d.txt", ".env.example"])
def test_ordinary_paths_may_be_written(path):
    assert patching.path_problem(path) is None


@pytest.mark.parametrize(
    "path",
    [
        "",
        "../escape.py",
        "src/../../etc/passwd",
        "src//double.py",
        "./x/./y.py",
        "back\\slash.py",
        "has\x00nul.py",
        "angle<bracket>.py",
        "a" * 400,
        ".git/config",
        ".github/workflows/ci.yml",
        ".env",
        "config/.env.production",
        "keys/server.pem",
        "secrets/token.txt",
        "uv.lock",
        "node_modules/x/index.js",
        "image.png",
    ],
)
def test_paths_the_tool_must_never_write_are_rejected(path):
    assert patching.path_problem(path)


# ---- applying edits -----------------------------------------------------------------


def test_edits_replace_exact_text():
    assert apply_edits(LOGIN, [edit("    return email\n", "    return email.strip()\n")]) == (
        "def parse(email):\n    return email.strip()\n\n\ndef other():\n    return 1\n"
    )


def test_edits_apply_in_order_each_to_the_previous_result():
    out = apply_edits("a = 1\n", [edit("a = 1", "a = 2"), edit("a = 2", "a = 3")])
    assert out == "a = 3\n"


def test_replace_may_delete_text():
    assert apply_edits("keep\nremove me\nkeep\n", [edit("remove me\n", "")]) == "keep\nkeep\n"


@pytest.mark.parametrize(
    ("edits", "message"),
    [
        ([edit("", "x")], "'search' is empty"),
        ([edit("same", "same")], "identical"),
        ([edit("not there", "x")], "was not found"),
        ([edit("return", "x")], "appears 2 times"),
        ([edit("return 1", "return 2"), edit("zzz", "x")], "edit 2"),
    ],
)
def test_bad_edits_explain_how_to_fix_them(edits, message):
    with pytest.raises(ValueError, match=message):
        apply_edits(LOGIN, edits)


def test_a_search_must_match_whitespace_exactly():
    with pytest.raises(ValueError, match="not found"):
        apply_edits(LOGIN, [edit("\treturn email", "x")])  # a tab; the file uses spaces


# ---- building a patch ---------------------------------------------------------------


def test_a_valid_modify_becomes_a_patch_with_a_diff_and_counts():
    patch, problems = patch_for(
        output(modify("src/login.py", edit("    return email\n", "    return email.strip()\n")))
    )
    assert problems == []
    assert patch is not None
    assert (patch.files_changed, patch.lines_added, patch.lines_removed) == (1, 1, 1)
    assert patch.base_commit_sha == BASE and patch.summary == "Does a thing."
    change = patch.changes[0]
    assert change.action == "modify" and change.path == "src/login.py"
    assert "--- a/src/login.py" in change.diff and "+++ b/src/login.py" in change.diff
    assert "-    return email\n" in change.diff and "+    return email.strip()\n" in change.diff
    assert change.edits[0].search == "    return email\n"  # kept so it can be re-applied


def test_a_new_file_is_created_with_a_diff_from_nothing():
    new = FileChangeOut(
        path="src/new.py", action="create", edits=[], content="x = 1\ny = 2\n", reason="r"
    )
    patch, problems = patch_for(output(new))
    assert problems == [] and patch is not None
    change = patch.changes[0]
    assert (change.lines_added, change.lines_removed) == (2, 0)
    assert "--- /dev/null" in change.diff and "+++ b/src/new.py" in change.diff
    assert change.content == "x = 1\ny = 2\n"


def test_a_file_can_be_deleted():
    gone = FileChangeOut(path="src/login.py", action="delete", edits=[], content="", reason="r")
    patch, problems = patch_for(output(gone))
    assert problems == [] and patch is not None
    assert patch.changes[0].lines_removed == 6 and "+++ /dev/null" in patch.changes[0].diff


def test_paths_are_normalised():
    patch, _ = patch_for(output(modify("./src/login.py", edit("return 1", "return 2"))))
    assert patch is not None and patch.changes[0].path == "src/login.py"


def test_a_missing_trailing_newline_is_marked_in_the_diff():
    read = files(a_dot_txt="old")
    patch, _ = patch_for(output(modify("a.txt", edit("old", "new"))), read)
    assert patch is not None
    assert "\\ No newline at end of file" in patch.changes[0].diff


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (modify("src/missing.py", edit("a", "b")), "does not exist"),
        (modify("src/login.py"), "at least one edit"),
        (modify("src/login.py", edit("nope", "x")), "was not found"),
        (
            FileChangeOut(
                path="src/login.py", action="create", edits=[], content="x\n", reason="r"
            ),
            "already exists",
        ),
        (
            FileChangeOut(path="src/new.py", action="create", edits=[], content="  \n", reason="r"),
            "empty file",
        ),
        (
            FileChangeOut(path="src/gone.py", action="delete", edits=[], content="", reason="r"),
            "does not exist",
        ),
        (modify(".env", edit("a", "b")), "never changed"),
        (modify("../x.py", edit("a", "b")), "must not contain"),
        (modify(".github/workflows/ci.yml", edit("a", "b")), "workflows"),
    ],
)
def test_invalid_changes_are_rejected_with_a_reason(change, message):
    patch, problems = patch_for(output(change))
    assert patch is None
    assert any(message in str(p) for p in problems)


def test_every_problem_is_reported_at_once():
    patch, problems = patch_for(
        output(
            modify("src/missing.py", edit("a", "b")),
            modify("src/login.py", edit("nope", "x")),
            modify(".env", edit("a", "b")),
        )
    )
    assert patch is None and len(problems) == 3


def test_the_same_path_twice_is_rejected():
    patch, problems = patch_for(
        output(
            modify("src/login.py", edit("return 1", "return 2")),
            modify("./src/login.py", edit("a", "b")),
        )
    )
    assert patch is None and any("more than once" in str(p) for p in problems)


def test_too_many_files_is_rejected():
    many = {f"f{i}_dot_py": "a\n" for i in range(MAX_CHANGED_FILES + 1)}
    changes = [modify(f"f{i}.py", edit("a", "b")) for i in range(MAX_CHANGED_FILES + 1)]
    patch, problems = patch_for(output(*changes), files(**many))
    assert patch is None and any("too many files" in str(p) for p in problems)


def test_a_huge_patch_is_rejected():
    big = "".join(f"line {i}\n" for i in range(MAX_CHANGED_LINES + 10))
    new = FileChangeOut(path="big.txt", action="create", edits=[], content=big, reason="r")
    patch, problems = patch_for(output(new))
    assert patch is None and any("over the limit" in str(p) for p in problems)


def test_a_new_file_that_looks_like_a_credential_is_rejected():
    secret = FileChangeOut(
        path="config.py",
        action="create",
        edits=[],
        content="KEY = 'AKIAIOSFODNN7EXAMPLE'\n",
        reason="r",
    )
    patch, problems = patch_for(output(secret))
    assert patch is None and any("credential" in str(p) for p in problems)


def test_an_edit_that_adds_a_credential_is_rejected_but_existing_ones_do_not_block_edits():
    read = files(a_dot_py="x = 1\n", b_dot_py="KEY = 'AKIAIOSFODNN7EXAMPLE'\ny = 1\n")
    adds = modify("a.py", edit("x = 1", "x = 'AKIAIOSFODNN7EXAMPLE'"))
    patch, problems = patch_for(output(adds), read)
    assert patch is None and any("credential" in str(p) for p in problems)

    unrelated = modify("b.py", edit("y = 1", "y = 2"))  # the key was already there
    patch, problems = patch_for(output(unrelated), read)
    assert problems == [] and patch is not None


def test_a_file_that_cannot_be_read_is_a_problem_not_a_crash():
    def read(path):
        raise RepositoryAccessError("src/login.py is too large to read")

    patch, problems = patch_for(output(modify("src/login.py", edit("a", "b"))), read)
    assert patch is None and "too large to read" in str(problems[0])


def test_no_changes_is_a_valid_patch_with_open_questions():
    patch, problems = patch_for(output(questions=["Which email format?"]))
    assert problems == [] and patch is not None
    assert patch.files_changed == 0 and patch.open_questions == ["Which email format?"]


def test_attempts_are_recorded():
    patch, _ = patch_for(output(modify("src/login.py", edit("return 1", "return 2"))), attempts=2)
    assert patch is not None and patch.attempts == 2
