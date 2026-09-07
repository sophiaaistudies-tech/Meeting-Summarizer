"""
Tests for the web layer. The three pipeline stages are replaced with fakes,
so these run without API keys and without sending mail.

    pytest -q
"""

import io
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

# Set before app import. notify.py reads ADMIN_EMAIL at import time, and an empty
# value makes notify_admin return before it ever opens an SMTP connection.
os.environ["ADMIN_EMAIL"] = ""
os.environ["APP_PASSCODE"] = "test-code"
os.environ["MAX_UPLOAD_MB"] = "1"
os.environ["MAX_JOBS_PER_HOUR"] = "100"

import app as webapp  # noqa: E402

SENT = []


NOTIFIED = []


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    """
    Nothing in this suite may touch the network or send mail.

    notify_admin is replaced for every test, not only the ones that assert on it.
    Without this, any test that runs a job would call the real notifier, which
    reads ADMIN_EMAIL from .env and sends genuine email through Gmail.
    """
    SENT.clear()
    NOTIFIED.clear()
    monkeypatch.setattr(webapp, "notify_admin",
                        lambda subject, fields: NOTIFIED.append((subject, fields)))
    monkeypatch.setattr(webapp, "transcribe", lambda p: "speaker_0: გამარჯობა")
    monkeypatch.setattr(
        webapp, "summarize",
        lambda t: {"title": "ტესტი", "summary": "ok", "decisions": [],
                   "action_items": [], "unresolved": []},
    )
    monkeypatch.setattr(webapp, "send", lambda d, t, r: SENT.append(r))
    with webapp._jobs_lock:
        webapp._jobs.clear()
    with webapp._rate_lock:
        webapp._rate.clear()


@pytest.fixture
def client():
    return TestClient(webapp.app)


def upload(client, **kw):
    data = {"passcode": kw.get("passcode", "test-code"),
            "recipients": kw.get("recipients", "a@b.ge")}
    name = kw.get("filename", "meeting.m4a")
    content = kw.get("content", b"\x00" * 2048)
    return client.post("/api/jobs", data=data,
                       files={"file": (name, io.BytesIO(content), "audio/mp4")})


def wait_done(client, job_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/jobs/{job_id}").json()
        if d["stage"] in ("done", "failed"):
            return d
        time.sleep(0.05)
    raise AssertionError("job did not finish")


# ---------- page and health ----------

def test_page_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "შეხვედრის შეჯამება" in r.text
    assert 'id="drop"' in r.text and 'id="passcode"' in r.text


def test_healthz(client):
    d = client.get("/healthz").json()
    assert d["ok"] is True and d["passcode_set"] is True


# ---------- happy path ----------

def test_full_job_sends_mail(client):
    r = upload(client, recipients="one@b.ge, two@c.ge")
    assert r.status_code == 200
    d = wait_done(client, r.json()["job_id"])
    assert d["stage"] == "done", d
    assert d["title"] == "ტესტი"
    assert SENT == [["one@b.ge", "two@c.ge"]]


def test_temp_file_is_deleted(client, monkeypatch):
    seen = {}

    def spy(path):
        seen["path"] = path
        assert os.path.exists(path), "file should exist during processing"
        return "speaker_0: ok"

    monkeypatch.setattr(webapp, "transcribe", spy)
    r = upload(client)
    wait_done(client, r.json()["job_id"])
    assert not os.path.exists(seen["path"]), "temp file must be removed after the job"


# ---------- access control ----------

def test_wrong_passcode_rejected(client):
    r = upload(client, passcode="nope")
    assert r.status_code == 401


def test_empty_passcode_rejected(client):
    r = upload(client, passcode="")
    assert r.status_code == 401


def test_no_passcode_configured_disables_uploads(client, monkeypatch):
    monkeypatch.setattr(webapp, "APP_PASSCODE", "")
    r = upload(client)
    assert r.status_code == 503


# ---------- validation ----------

@pytest.mark.parametrize("bad", ["", "   ", "notanemail", "a@b", "a@b.ge, broken@"])
def test_bad_recipients_rejected(client, bad):
    assert upload(client, recipients=bad).status_code == 400


def test_too_many_recipients(client):
    many = ", ".join(f"p{i}@b.ge" for i in range(6))
    assert upload(client, recipients=many).status_code == 400


def test_recipient_domain_allowlist(client, monkeypatch):
    monkeypatch.setattr(webapp, "ALLOWED_DOMAINS", ["gah.ge"])
    assert upload(client, recipients="x@gmail.com").status_code == 400
    assert upload(client, recipients="x@gah.ge").status_code == 200


def test_bad_extension_rejected(client):
    assert upload(client, filename="notes.pdf").status_code == 400
    assert upload(client, filename="script.exe").status_code == 400


def test_empty_file_rejected(client):
    assert upload(client, content=b"").status_code == 400


def test_oversized_file_rejected(client):
    # MAX_UPLOAD_MB is 1 in this test run
    r = upload(client, content=b"\x00" * (2 * 1024 * 1024))
    assert r.status_code == 413


def test_recipients_accept_semicolons_and_newlines(client):
    r = upload(client, recipients="one@b.ge;\n two@c.ge")
    assert r.status_code == 200
    wait_done(client, r.json()["job_id"])
    assert SENT == [["one@b.ge", "two@c.ge"]]


# ---------- failure reporting ----------

def test_pipeline_error_surfaces_to_client(client, monkeypatch):
    def boom(p):
        raise RuntimeError("ElevenLabs quota exceeded")

    monkeypatch.setattr(webapp, "transcribe", boom)
    r = upload(client)
    d = wait_done(client, r.json()["job_id"])
    assert d["stage"] == "failed"
    assert "quota exceeded" in d["error"]


def test_silent_recording_reported(client, monkeypatch):
    monkeypatch.setattr(webapp, "transcribe", lambda p: "   ")
    r = upload(client)
    d = wait_done(client, r.json()["job_id"])
    assert d["stage"] == "failed"
    assert "No speech" in d["error"]


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/deadbeef").status_code == 404


# ---------- rate limiting ----------

def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(webapp, "MAX_JOBS_PER_HOUR", 2)
    assert upload(client).status_code == 200
    assert upload(client).status_code == 200
    assert upload(client).status_code == 429


# ---------- usage notifications ----------

def test_notification_sent_on_success(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(webapp, "notify_admin",
                        lambda subject, fields: seen.update(subject=subject, fields=fields))
    r = upload(client, filename="board.m4a", recipients="a@b.ge")
    wait_done(client, r.json()["job_id"])
    assert "worked" in seen["subject"]
    f = seen["fields"]
    assert f["Outcome"] == "worked"
    assert f["Meeting"] == "ტესტი"
    assert f["Sent to"] == "a@b.ge"
    assert f["Recording"] == "board.m4a"
    assert f["Size"].endswith("MB")
    assert f["Took"].endswith("s")
    assert f["Error"] == ""


def test_notification_sent_on_failure(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(webapp, "notify_admin",
                        lambda subject, fields: seen.update(subject=subject, fields=fields))
    monkeypatch.setattr(webapp, "transcribe",
                        lambda p: (_ for _ in ()).throw(RuntimeError("quota gone")))
    r = upload(client)
    wait_done(client, r.json()["job_id"])
    assert "failed" in seen["subject"]
    assert "quota gone" in seen["fields"]["Error"]
    assert seen["fields"]["Meeting"] == ""


def test_failed_notification_does_not_break_the_job(client, monkeypatch):
    def broken(subject, fields):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(webapp, "notify_admin", broken)
    r = upload(client)
    d = wait_done(client, r.json()["job_id"])
    # the summary itself still succeeded and the mail still went out
    assert d["stage"] == "done", d
    assert SENT == [["a@b.ge"]]


def test_notifications_off_when_admin_email_unset(monkeypatch):
    import notify
    monkeypatch.setattr(notify, "ADMIN_EMAIL", "")
    assert notify.notify_admin("x", {"a": "b"}) is False


# ---------- client IP behind a proxy ----------

def test_forwarded_header_ignored_when_proxy_not_trusted(client, monkeypatch):
    monkeypatch.setattr(webapp, "TRUST_PROXY", False)
    monkeypatch.setattr(webapp, "MAX_JOBS_PER_HOUR", 1)
    h1 = {"X-Forwarded-For": "1.1.1.1"}
    h2 = {"X-Forwarded-For": "2.2.2.2"}
    assert client.post("/api/jobs", data={"passcode": "test-code", "recipients": "a@b.ge"},
                       files={"file": ("a.m4a", io.BytesIO(b"x" * 999), "audio/mp4")},
                       headers=h1).status_code == 200
    # different forwarded IP, but the header is not trusted, so same bucket
    assert client.post("/api/jobs", data={"passcode": "test-code", "recipients": "a@b.ge"},
                       files={"file": ("a.m4a", io.BytesIO(b"x" * 999), "audio/mp4")},
                       headers=h2).status_code == 429


def test_forwarded_header_used_when_proxy_trusted(client, monkeypatch):
    monkeypatch.setattr(webapp, "TRUST_PROXY", True)
    monkeypatch.setattr(webapp, "MAX_JOBS_PER_HOUR", 1)
    mk = lambda ip: dict(data={"passcode": "test-code", "recipients": "a@b.ge"},
                         files={"file": ("a.m4a", io.BytesIO(b"x" * 999), "audio/mp4")},
                         headers={"X-Forwarded-For": ip})
    assert client.post("/api/jobs", **mk("1.1.1.1")).status_code == 200
    # a different visitor gets their own allowance
    assert client.post("/api/jobs", **mk("2.2.2.2")).status_code == 200
    # the first visitor is now over their limit
    assert client.post("/api/jobs", **mk("1.1.1.1")).status_code == 429


def test_forwarded_chain_takes_the_original_client(client, monkeypatch):
    monkeypatch.setattr(webapp, "TRUST_PROXY", True)
    seen = {}
    monkeypatch.setattr(webapp, "notify_admin",
                        lambda subject, fields: seen.update(fields=fields))
    r = client.post("/api/jobs", data={"passcode": "test-code", "recipients": "a@b.ge"},
                    files={"file": ("a.m4a", io.BytesIO(b"x" * 999), "audio/mp4")},
                    headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.1, 172.16.0.1"})
    wait_done(client, r.json()["job_id"])
    assert seen["fields"]["From IP"] == "9.9.9.9"


# ---------- the suite must never send real mail ----------

def test_no_test_can_reach_smtp(client, monkeypatch):
    """
    A regression guard. An earlier version of this suite left the real notifier in
    place for most tests, so running pytest sent a burst of genuine emails through
    the operator's Gmail account. Any SMTP connection from a test now fails loudly.
    """
    import smtplib

    def forbidden(*a, **k):
        raise AssertionError("a test tried to open an SMTP connection")

    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden)
    monkeypatch.setattr(smtplib, "SMTP", forbidden)

    r = upload(client, recipients="a@b.ge")
    d = wait_done(client, r.json()["job_id"])
    assert d["stage"] == "done"
    # the notification was recorded by the fake, not sent
    assert len(NOTIFIED) == 1
    assert NOTIFIED[0][0] == "Meeting summarizer: worked"


def test_real_notifier_is_inert_without_admin_email():
    """notify.py must not attempt SMTP when ADMIN_EMAIL is empty, as it is here."""
    import smtplib
    import notify

    original = smtplib.SMTP_SSL
    try:
        smtplib.SMTP_SSL = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("notify_admin opened SMTP with no ADMIN_EMAIL")
        )
        assert notify.ADMIN_EMAIL == ""
        assert notify.notify_admin("subject", {"a": "b"}) is False
    finally:
        smtplib.SMTP_SSL = original
