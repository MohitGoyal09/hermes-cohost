import contextvars
import time

from cohost.notify import Notifier

session_var = contextvars.ContextVar("session", default="none")


class Handle:
    def __init__(self, log, text):
        self.log, self.text = log, text

    def edit(self, text):
        self.log.append(("edit", text, session_var.get()))
        return {"ok": True}


def test_status_card_edits_in_place_and_events_post_in_the_callers_context():
    log = []

    def post(text):
        log.append(("post", text, session_var.get()))
        return Handle(log, text)

    counter = iter(range(100))
    session_var.set("telegram:42")
    n = Notifier(post, status=lambda: f"status {next(counter)}", interval=0.05)
    n.start()
    n.send("🔥 Highlight")
    time.sleep(0.2)
    n.stop("final")
    assert log[0] == ("post", "status 0", "telegram:42")
    assert ("post", "🔥 Highlight", "telegram:42") in log
    assert any(kind == "edit" for kind, *_ in log)
    assert log[-1][:2] == ("edit", "final")
    assert {ctx for *_, ctx in log} == {"telegram:42"}


def test_post_errors_do_not_kill_the_loop():
    calls = []

    def post(text):
        calls.append(text)
        raise RuntimeError("gateway down")

    n = Notifier(post, status=lambda: "s", interval=0.02)
    n.start()
    n.send("a")
    time.sleep(0.1)
    n.stop("done")
    assert "a" in calls
