import os
import re
import time

from fixtures.builders import cc_assistant, cc_user, usage, write_jsonl


def project_dir(cli):
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cli.repo.resolve()))
    return cli.home / ".claude" / "projects" / slug


def make_session(cli, name, out_tokens, prompt="go", age=0, uuid_prefix=None):
    pre = uuid_prefix or name
    path = write_jsonl(project_dir(cli) / f"{name}.jsonl", [
        cc_user(prompt, 0, uuid=f"{pre}-u"),
        cc_assistant(f"{pre}-m", 5, usage=usage(1, out_tokens), uuid=f"{pre}-a"),
    ])
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def test_session_id_selects_the_transcript(cli):
    make_session(cli, "aaa", 111)
    make_session(cli, "bbb", 222)
    code, out = cli.json("--session-id", "bbb")
    assert code == 0
    assert out["tokens"]["output"] == 222


def test_two_recent_transcripts_are_listed_not_guessed(cli):
    make_session(cli, "aaa", 111, prompt="add rate limiting", age=60)
    make_session(cli, "bbb", 222, prompt="refactor the parser", age=120)
    code, out = cli.json()
    assert code == 3
    previews = {c["preview"] for c in out["ambiguous"]}
    assert previews == {"add rate limiting", "refactor the parser"}
    assert "tokens" not in out


def test_single_recent_transcript_is_used(cli):
    make_session(cli, "aaa", 111, age=30)
    make_session(cli, "old", 999, age=86400)
    code, out = cli.json()
    assert code == 0
    assert out["tokens"]["output"] == 111


def test_old_transcripts_do_not_make_it_ambiguous(cli):
    make_session(cli, "aaa", 111, age=30)
    make_session(cli, "bbb", 222, age=7200)
    code, out = cli.json()
    assert code == 0 and out["tokens"]["output"] == 111


def test_several_paths_merge_in_time_order_and_forks_count_once(cli, tmp_path):
    a = write_jsonl(tmp_path / "a.jsonl", [
        cc_user("go", 0, uuid="u1"), cc_assistant("m1", 5, usage=usage(1, 10), uuid="a1")])
    fork = write_jsonl(tmp_path / "fork.jsonl", [
        cc_user("go", 0, uuid="u1"), cc_assistant("m1", 5, usage=usage(1, 10), uuid="a1"),
        cc_assistant("m2", 50, usage=usage(1, 30), uuid="a2")])
    _, out = cli.json(a, fork, "--host", "claude-code")
    assert out["tokens"]["output"] == 40
    assert out["tokens"]["api_calls"] == 2


def test_subagent_transcripts_are_discovered_and_attached_to_their_turn(cli, tmp_path):
    main = write_jsonl(tmp_path / "s1.jsonl", [
        cc_user("first task", 0, uuid="u1"),
        cc_assistant("m1", 5, usage=usage(1, 10), uuid="a1"),
        cc_user("second task", 100, uuid="u2"),
        cc_assistant("m2", 105, usage=usage(1, 20), uuid="a2"),
    ])
    write_jsonl(tmp_path / "s1" / "subagents" / "agent-7.jsonl", [
        cc_user("review the diff carefully", 110, uuid="su1"),
        cc_assistant("sm1", 115, usage=usage(5, 500), uuid="sa1", model="claude-opus-x"),
    ])
    _, out = cli.json(main, "--host", "claude-code")
    actors = {a["actor"]: a for a in out["actors"]}
    assert set(actors) == {"main", "subagent:agent-7"}
    assert actors["subagent:agent-7"]["tokens"]["output"] == 500
    assert actors["subagent:agent-7"]["brief_bytes"] == len("review the diff carefully")
    assert actors["subagent:agent-7"]["models"] == ["claude-opus-x"]
    assert out["turns"][1]["tokens"]["output"] == 520
    assert out["turns"][0]["tokens"]["output"] == 10
