import pytest

from fixtures.builders import (bash, cc_assistant, cc_bash_call, cc_tool_result, cc_user,
                               tool_use, usage, write_jsonl)


def run_session(cli, tmp_path, *steps, extra=()):
    records = [cc_user("do the task", 0)]
    for i, step in enumerate(steps):
        records += step
    path = write_jsonl(tmp_path / "s1.jsonl", records)
    return cli.json(path, "--host", "claude-code", *extra)[1]


def verify_total(cli, tmp_path, command, extra=()):
    out = run_session(cli, tmp_path, cc_bash_call("m1", "c1", command, 10), extra=extra)
    return out["verify"]["total"]


@pytest.mark.parametrize("command", [
    "A=1 B=$(mktemp -d) make verify",
    "(export X=1; time make verify)",
    "bash -c 'npm test'",
    "uv run pytest -q",
    "cd app && npx vitest run",
    "make lint | tail -5",
])
def test_real_verify_commands_are_counted(cli, tmp_path, command):
    assert verify_total(cli, tmp_path, command) == 1


@pytest.mark.parametrize("command", [
    'git commit -m "run make verify before merge"',
    "cat <<'EOF' > notes.md\nmake verify\nnpm test\nEOF",
    "echo 'pytest'",
    "ls tests",
])
def test_text_that_only_mentions_a_command_is_not_counted(cli, tmp_path, command):
    assert verify_total(cli, tmp_path, command) == 0


def test_custom_verify_pattern(cli, tmp_path):
    assert verify_total(cli, tmp_path, "./scripts/ci-check.sh --fast") == 0
    assert verify_total(cli, tmp_path, "./scripts/ci-check.sh --fast",
                        extra=("--verify-pattern", r"\./scripts/ci-check\.sh")) == 1


def test_verify_runs_before_the_last_code_edit_are_counted(cli, tmp_path):
    edit = [cc_assistant("e1", 30, [tool_use("Edit", {"file_path": "a.py"}, "ce")], usage(1, 1)),
            cc_tool_result("ce", "done", 31)]
    out = run_session(cli, tmp_path,
                      cc_bash_call("m1", "c1", "pytest", 10),
                      edit,
                      cc_bash_call("m2", "c2", "pytest", 40))
    assert out["verify"]["total"] == 2
    assert out["verify"]["before_last_edit"] == 1
    assert out["verify"]["by_kind"]["test"] == 2


def test_tool_calls_are_categorised_and_files_tracked(cli, tmp_path):
    def call(mid, cid, name, inp, t):
        return [cc_assistant(mid, t, [tool_use(name, inp, cid)], usage(1, 1)),
                cc_tool_result(cid, "x" * 50, t + 1)]
    out = run_session(
        cli, tmp_path,
        call("m1", "c1", "Read", {"file_path": "src/a.py"}, 10),
        call("m2", "c2", "Read", {"file_path": "src/a.py"}, 20),
        call("m3", "c3", "Grep", {"pattern": "x"}, 30),
        call("m4", "c4", "Edit", {"file_path": "src/a.py"}, 40),
        call("m5", "c5", "Edit", {"file_path": "src/a.py"}, 50),
        call("m6", "c6", "Task", {"prompt": "do it"}, 60),
        call("m7", "c7", "Skill", {"skill": "tdd"}, 70),
    )
    assert out["tools"]["by_category"] == {"read": 2, "search": 1, "edit": 2, "agent": 1, "skill": 1}
    assert out["tools"]["repeated_reads"] == {"src/a.py": 2}
    assert out["tools"]["edits_per_file"] == {"src/a.py": 2}


def test_failing_command_retried_and_errors_and_largest_outputs(cli, tmp_path):
    out = run_session(
        cli, tmp_path,
        cc_bash_call("m1", "c1", "pytest -x", 10, "F" * 5000, is_error=True),
        cc_bash_call("m2", "c2", "pytest -x", 20, "F" * 7000, is_error=True),
        cc_bash_call("m3", "c3", "ls", 30, "ok"),
    )
    assert out["tools"]["errors"] == 2
    assert out["tools"]["retried_failures"] == [{"command": "pytest -x", "times": 2}]
    assert out["tools"]["largest_outputs"][0]["bytes"] == 7000
    assert "text" not in out["tools"]["largest_outputs"][0]


def test_largest_outputs_name_the_tool_and_command(cli, tmp_path):
    out = run_session(cli, tmp_path, cc_bash_call("m1", "c1", "cat big.log", 10, "L" * 9000))
    top = out["tools"]["largest_outputs"][0]
    assert top["tool"] == "Bash" and top["command"] == "cat big.log"
