from fixtures.builders import cc_assistant, cc_user, usage, write_jsonl


def session(tmp_path, records, **kw):
    return write_jsonl(tmp_path / "s1.jsonl", records, **kw)


def test_split_records_sharing_a_message_id_are_counted_once(cli, tmp_path):
    path = session(tmp_path, [
        cc_user("add rate limiting", 0),
        cc_assistant("m1", 5, usage=usage(10, 5, 100, 20)),
        cc_assistant("m1", 6, usage=usage(10, 5, 100, 20)),   # split record, same id
        cc_assistant("m2", 20, usage=usage(3, 7, 50, 0)),
    ])
    code, out = cli.json(path, "--host", "claude-code")
    assert code == 0
    assert out["tokens"]["fresh_input"] == 13
    assert out["tokens"]["output"] == 12
    assert out["tokens"]["cache_read"] == 150
    assert out["tokens"]["cache_write"] == 20
    assert out["tokens"]["api_calls"] == 2


def test_usage_without_an_id_is_counted_and_flagged(cli, tmp_path):
    rec = cc_assistant("x", 5, usage=usage(4, 2))
    del rec["message"]["id"]
    path = session(tmp_path, [cc_user("go", 0), rec])
    _, out = cli.json(path, "--host", "claude-code")
    assert out["tokens"]["fresh_input"] == 4
    assert out["tokens"]["calls_without_id"] == 1
    assert out["tokens"]["api_calls"] == 1


def test_context_size_is_reported_at_the_last_api_call(cli, tmp_path):
    path = session(tmp_path, [
        cc_user("go", 0),
        cc_assistant("m1", 5, usage=usage(10, 5, 100, 20)),
        cc_assistant("m2", 9, usage=usage(2, 5, 300, 40)),
    ])
    _, out = cli.json(path, "--host", "claude-code")
    assert out["tokens"]["context_at_invocation"] == 342
