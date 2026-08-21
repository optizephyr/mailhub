from __future__ import annotations

from datetime import datetime

from mailhub.plugins.sources.qq_imap import QqImapSource


class _Message:
    def __init__(self, message_id: str) -> None:
        self.headers = {"message-id": message_id}
        self.subject = "【京东校招】测评通知"
        self.from_ = "campus@example.com"
        self.date = datetime(2026, 8, 21, 10, 0)
        self.text = "本次测评预计耗时90分钟。"
        self.html = ""
        self.uid = "42"


class _Mailbox:
    def __init__(self, messages: dict[str, _Message]) -> None:
        self.messages = messages
        self.fetch_calls: list[dict[str, object]] = []

    def login(self, *_args, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def fetch(self, criteria, **kwargs):
        self.fetch_calls.append({"criteria": criteria, **kwargs})
        raw = str(criteria)
        return (
            [message]
            if (message := next(
                (item for key, item in self.messages.items() if key in raw),
                None,
            ))
            else []
        )


def test_fetch_by_message_ids_fetches_exact_unseen_messages(monkeypatch):
    mailbox = _Mailbox({"wanted@qq.com": _Message("<wanted@qq.com>")})
    monkeypatch.setattr(
        "mailhub.plugins.sources.qq_imap.MailBox",
        lambda _host: mailbox,
    )
    source = QqImapSource("a@qq.com", "secret")

    messages = source.fetch_by_message_ids(
        ["<wanted@qq.com>", "<wanted@qq.com>", "local-1-fallback"]
    )

    assert [message.source.message_id for message in messages] == ["<wanted@qq.com>"]
    assert len(mailbox.fetch_calls) == 1
    assert mailbox.fetch_calls[0]["mark_seen"] is False
    assert mailbox.fetch_calls[0]["limit"] == 1
