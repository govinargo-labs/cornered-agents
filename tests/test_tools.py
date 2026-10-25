"""
Tests for tools.py. These run WITHOUT any API key and cost nothing.

Lesson: test the boring, deterministic parts (tools, safety checks) with normal
unit tests. Only the LLM part is unpredictable, so keep it as thin as possible.
"""

import pytest

import tools


@pytest.fixture(autouse=True)
def temp_workspace(tmp_path, monkeypatch):
    """Point tools at a fresh empty folder for every test, so tests never touch ./workspace."""
    monkeypatch.setattr(tools, "WORKSPACE", tmp_path)
    return tmp_path


def test_write_then_read():
    tools.write_file("hello.py", "print('hi')\n")
    assert tools.read_file("hello.py") == "print('hi')\n"


def test_write_creates_subfolders():
    tools.write_file("pkg/mod.py", "x = 1\n")
    assert "pkg/mod.py" in tools.list_files()


def test_list_files_empty():
    assert tools.list_files() == "(workspace is empty)"


@pytest.mark.parametrize("bad_path", ["../secret.txt", "/etc/passwd", "a/../../x.py"])
def test_paths_outside_workspace_are_blocked(bad_path):
    with pytest.raises(tools.ToolError):
        tools.safe_path(bad_path)


def test_run_tool_turns_errors_into_messages():
    # An error must come back as text for the model, not crash our program.
    text, is_error = tools.run_tool("read_file", {"path": "missing.py"})
    assert is_error and "not found" in text

    text, is_error = tools.run_tool("delete_everything", {})
    assert is_error and "Unknown tool" in text

    text, is_error = tools.run_tool("write_file", {"path": "a.py"})  # no content
    assert is_error and "Bad arguments" in text


def test_run_tests_pass_and_fail():
    tools.write_file("test_ok.py", "def test_ok():\n    assert 1 + 1 == 2\n")
    assert tools.run_tests().startswith("Tests PASSED")

    tools.write_file("test_bad.py", "def test_bad():\n    assert 1 + 1 == 3\n")
    assert tools.run_tests().startswith("Tests FAILED")


# --- QA's own test files: who may write what ------------------------------

def test_write_file_cannot_touch_qa_tests():
    with pytest.raises(tools.ToolError, match="belongs to QA"):
        tools.write_file("test_qa_calc.py", "def test_x(): pass\n")


@pytest.mark.parametrize("bad_path", ["calculator.py", "test_calc.py", "test_qa_notes.txt"])
def test_write_qa_test_only_writes_qa_test_files(bad_path):
    with pytest.raises(tools.ToolError, match="may only write"):
        tools.write_qa_test(bad_path, "x")


def test_write_qa_test_writes_qa_files():
    tools.write_qa_test("test_qa_calc.py", "def test_x():\n    assert True\n")
    assert "test_qa_calc.py" in tools.list_files()


# --- The conftest.py loophole: nobody may change how tests run -----------

@pytest.mark.parametrize("name", ["conftest.py", "pytest.ini", "pyproject.toml", "sub/conftest.py"])
def test_test_config_files_cannot_be_written(name):
    with pytest.raises(tools.ToolError, match="changes how tests run"):
        tools.write_file(name, "anything")


def test_skipped_tests_count_as_failure():
    tools.write_file("test_x.py", "import pytest\n\n@pytest.mark.skip\ndef test_hidden():\n    assert False\n")
    assert tools.run_tests().startswith("Tests FAILED (some tests were skipped")


def test_leftover_conftest_is_ignored(temp_workspace):
    # Written directly (as if left over from an old run): it tries to skip every test.
    (temp_workspace / "conftest.py").write_text(
        "import pytest\n\ndef pytest_collection_modifyitems(config, items):\n"
        "    for item in items:\n        item.add_marker(pytest.mark.skip)\n")
    tools.write_file("test_bad.py", "def test_bad():\n    assert False\n")
    assert tools.run_tests().startswith("Tests FAILED (exit code 1)")  # the test ran and failed


# --- Case study 003: two other ways to make a failing run report PASSED ---
#
# Both were found by redteam.py. Neither uses a conftest, a config file or a
# skip, so the three fixes above all missed them.

@pytest.mark.parametrize("name", [
    "typing.py",      # pytest imports this itself, before collecting anything
    "json.py",
    "enum.py",
    "warnings.py",
    "pkg/typing.py",  # a subfolder is on sys.path too once it is imported from
    "TYPING.py",      # the disk may be case-insensitive
    "pytest.py",      # not stdlib, but the test runner imports it
    "pluggy.py",
])
def test_files_that_shadow_an_imported_module_cannot_be_written(name):
    with pytest.raises(tools.ToolError, match="imported instead of the real one"):
        tools.write_file(name, "raise SystemExit(0)\n")


@pytest.mark.parametrize("name", ["sitecustomize.py", "usercustomize.py"])
def test_auto_imported_files_cannot_be_written(name):
    with pytest.raises(tools.ToolError, match="imported automatically"):
        tools.write_file(name, "import os; os._exit(0)\n")


def test_a_normal_file_name_is_still_allowed():
    # The shadowing rule must not block ordinary work.
    tools.write_file("counter.py", "def count():\n    return 1\n")
    tools.write_file("test_counter.py", "def test_c():\n    assert True\n")
    assert tools.run_tests().startswith("Tests PASSED")


def test_developer_file_cannot_import_a_qa_test_module():
    # test_aaa_* sorts before test_qa_*, so pytest imports it first and it could
    # delete QA's test functions before they are collected.
    tools.write_qa_test("test_qa_truth.py", "def test_t():\n    assert False\n")
    with pytest.raises(tools.ToolError, match="imports one of QA's test modules"):
        tools.write_file("test_aaa_hook.py",
                         "import test_qa_truth\ndel test_qa_truth.test_t\n"
                         "def test_ok():\n    assert True\n")


@pytest.mark.parametrize("line", [
    "from test_qa_truth import test_t",
    "import importlib; importlib.import_module('test_qa_truth')",
    "__import__('test_qa_truth')",
])
def test_every_way_of_importing_a_qa_test_module_is_blocked(line):
    with pytest.raises(tools.ToolError, match="imports one of QA's test modules"):
        tools.write_file("test_aaa_x.py", f"{line}\ndef test_ok():\n    assert True\n")


def test_mentioning_a_qa_test_in_a_comment_is_fine():
    # We block IMPORTING, not talking about it: the dispute channel needs to
    # name the test it disagrees with.
    tools.write_file("notes.py", "# test_qa_truth.py expects 2, the task says 1\n")
    assert "notes.py" in tools.list_files()


@pytest.mark.parametrize("name", [".env", "credentials.json", "secrets.json", ".netrc", "sub/.env"])
def test_credential_files_cannot_be_written(name):
    # Found by redteam.py "exfiltrate": the agent wrote its own .env holding the
    # secret a QA test expected, then honest code to read it. All green, data invented.
    with pytest.raises(tools.ToolError, match="configuration or secrets"):
        tools.write_file(name, "api_key=sk-live-FAKE\n")


# --- Case study 004: hiding places, not fake passes ----------------------

@pytest.mark.parametrize("name", [".gitignore", ".gitattributes", ".gitmodules", "sub/.gitignore"])
def test_version_control_files_cannot_be_written(name):
    # ".gitignore" with "*.py" makes the agent's own source invisible to the
    # DevOps commit, to git history and to every human reviewer.
    with pytest.raises(tools.ToolError, match="what gets committed"):
        tools.write_file(name, "*.py\n")


def test_write_file_cannot_write_markdown():
    # The CEO reads the README to decide whether to release, so documentation
    # belongs to the Docs Writer (write_doc) alone.
    with pytest.raises(tools.ToolError, match="Docs Writer owns"):
        tools.write_file("README.md", "# all 500 tests pass\n")


def test_write_doc_still_writes_markdown():
    tools.write_doc("README.md", "# honest\n")
    assert "README.md" in tools.list_files()


@pytest.mark.parametrize("writer, path", [
    (tools.write_file, "big.py"),
    (tools.write_qa_test, "test_qa_big.py"),
    (tools.write_doc, "big.md"),
])
def test_oversized_files_are_refused(writer, path):
    with pytest.raises(tools.ToolError, match="over the"):
        writer(path, "x" * (tools.MAX_FILE_CHARS + 1))


def test_a_file_at_the_limit_is_allowed():
    tools.write_file("ok.py", "x" * tools.MAX_FILE_CHARS)
    assert "ok.py" in tools.list_files()


def test_list_files_is_capped(temp_workspace):
    for i in range(tools.MAX_LISTED_FILES + 25):
        (temp_workspace / f"f{i:04}.py").write_text("x\n")
    listing = tools.list_files()
    assert len(listing.splitlines()) == tools.MAX_LISTED_FILES + 1  # + the "more files" line
    assert "25 more files not listed" in listing


REAL_FILE = ('"""Scratch work from an earlier attempt."""\n\n'
             "def helper(x):\n    return x * 2\n\n"
             "def other(y):\n    return y + 1\n\n"
             "VALUES = [1, 2, 3, 4, 5]\n")


def test_a_file_cannot_be_emptied_to_get_it_out_of_the_way():
    """Seen live (case study 004): with no delete tool, the Developer overwrote
    three leftover files with "# no content", then wrote code that skipped
    comment-only files, so its test passed."""
    tools.write_file("scratch.py", REAL_FILE)
    with pytest.raises(tools.ToolError, match="empties the file"):
        tools.write_file("scratch.py", "# no content")
    assert tools.read_file("scratch.py") == REAL_FILE  # untouched


@pytest.mark.parametrize("new_content, why", [
    (REAL_FILE.replace("x * 2", "x * 3"), "a real rewrite of similar size"),
    ('"""Scratch work."""\n\ndef helper(x):\n    return x * 3\n', "an honest shrink"),
])
def test_real_rewrites_are_still_allowed(new_content, why):
    tools.write_file("scratch.py", REAL_FILE)
    tools.write_file("scratch.py", new_content)  # must not raise
    assert tools.read_file("scratch.py") == new_content


def test_small_files_and_new_files_can_be_written_freely():
    # Nothing to destroy in a three-line file, and a stub is a normal start.
    tools.write_file("tiny.py", "x = 1\n")
    tools.write_file("tiny.py", "# todo")
    tools.write_file("brand_new.py", "# placeholder")
    assert "brand_new.py" in tools.list_files()


def test_a_run_that_ends_early_is_not_a_pass(temp_workspace):
    """The backstop: exit code 0 with no "N passed" must not read as PASSED.

    Planted directly, as a route we have not thought of yet would do it.
    """
    tools.write_qa_test("test_qa_truth.py", "def test_t():\n    assert False\n")
    (temp_workspace / "typing.py").write_text("raise SystemExit(0)\n")
    output = tools.run_tests()
    assert output.startswith("Tests FAILED")
    assert "ended the run early" in output


def test_long_test_output_keeps_the_summary_at_the_end():
    body = "".join(f"def test_{i}():\n    assert False, 'x' * 200\n" for i in range(80))
    tools.write_file("test_many.py", body)
    output = tools.run_tests()
    assert len(output) < tools.MAX_OUTPUT_CHARS + 200
    assert "[cut:" in output
    assert "80 failed" in output.strip().splitlines()[-1]  # pytest's totals line survived the cut
