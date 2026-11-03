import json
import logging
import sys

from wewln_phonecaller.logging_setup import (
    JsonFormatter,
    TextFormatter,
    call_id_var,
    request_id_var,
    setup_logging,
    shutdown_logging,
)

SECRET = "s" * 40


def record(msg, *args, **extra):
    rec = logging.LogRecord("wewln.test", logging.INFO, __file__, 1, msg, args, None)
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


def test_json_masks_phone_in_message_and_arguments():
    out = JsonFormatter().format(record("calling %s", "+7 916 123-45-67"))
    data = json.loads(out)
    assert data["msg"] == "calling +79*******67"
    assert "916 123" not in out
    assert data["level"] == "INFO"
    assert data["logger"] == "wewln.test"


def test_json_masks_extra_fields():
    out = JsonFormatter().format(record("x", phone="+79161234567", nested={"to": "89161234567"}))
    data = json.loads(out)
    assert data["phone"] == "+79*******67"
    assert data["nested"] == {"to": "89*******67"}


def test_json_masks_known_secrets():
    out = JsonFormatter(secrets=[SECRET]).format(record("token is %s", SECRET))
    assert SECRET not in out


def test_json_includes_correlation_ids():
    rid, cid = request_id_var.set("req-1"), call_id_var.set("call-1")
    try:
        data = json.loads(JsonFormatter().format(record("hello")))
    finally:
        request_id_var.reset(rid)
        call_id_var.reset(cid)
    assert data["request_id"] == "req-1"
    assert data["call_id"] == "call-1"


def test_exception_text_is_masked():
    rec = record("failed")
    try:
        raise ValueError("bad number +79161234567")
    except ValueError:
        rec.exc_info = sys.exc_info()
    data = json.loads(JsonFormatter().format(rec))
    assert "+79*******67" in data["exc"]
    assert "+79161234567" not in data["exc"]


def test_text_formatter_masks():
    out = TextFormatter(secrets=[SECRET]).format(record("to %s with %s", "89161234567", SECRET))
    assert "89*******67" in out
    assert SECRET not in out


def test_log_file_is_utf8_json_and_masked(tmp_path):
    log_file = tmp_path / "logs" / "app.log"
    setup_logging(level="INFO", fmt="json", log_file=log_file)
    logging.getLogger("wewln.test").info("Звонок на %s завершён", "+7 916 123-45-67")
    shutdown_logging()
    data = json.loads(log_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert data["msg"] == "Звонок на +79*******67 завершён"


def test_setup_logging_replaces_its_own_handlers(tmp_path):
    root = logging.getLogger()
    before = len(root.handlers)
    setup_logging(log_file=tmp_path / "a.log")
    setup_logging(log_file=tmp_path / "a.log")
    assert len(root.handlers) == before + 2
    shutdown_logging()
    assert len(root.handlers) == before
