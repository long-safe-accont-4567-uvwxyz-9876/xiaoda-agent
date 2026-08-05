from cli_app import collect_reply


def test_collect_reply_returns_final():
    events = [
        {"type": "status", "text": "thinking", "msg_id": "m1"},
        {"type": "final", "reply": "你好", "msg_id": "m1"},
    ]
    assert collect_reply(events, "m1") == ("你好", None)


def test_collect_reply_ignores_other_msg_and_error_short_circuits():
    events = [
        {"type": "final", "reply": "别的", "msg_id": "other"},
        {"type": "error", "message": "出错了", "msg_id": "m1"},
        {"type": "final", "reply": "你好", "msg_id": "m1"},
    ]
    assert collect_reply(events, "m1") == ("", "出错了")


def test_collect_reply_empty():
    assert collect_reply([], "m1") == ("", None)