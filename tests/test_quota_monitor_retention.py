from datetime import datetime, timedelta, timezone
import sqlite3

from quota_monitor import app as quota_app


def _insert_capture(conn: sqlite3.Connection, captured_at: str, screenshot_path: str | None) -> None:
    conn.execute(
        "INSERT INTO captures(provider,captured_at,url,text,fields_json,status,confidence,screenshot_path,screenshot_sha256,error,reset_key) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("codex", captured_at, "https://example.invalid", "", "{}", "healthy", 1.0,
         screenshot_path, None, "", None),
    )


def test_prune_expired_removes_old_capture_and_screenshot(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    screenshot_dir = data_dir / "screenshots"
    db_path = data_dir / "quota.sqlite3"
    monkeypatch.setattr(quota_app, "DATA_DIR", data_dir)
    monkeypatch.setattr(quota_app, "SCREENSHOT_DIR", screenshot_dir)
    monkeypatch.setattr(quota_app, "DB_PATH", db_path)
    monkeypatch.setattr(quota_app, "RETENTION_DAYS", 7)

    conn = quota_app._db()
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    old_path = screenshot_dir / "old.png"
    new_path = screenshot_dir / "new.png"
    old_path.write_bytes(b"old")
    new_path.write_bytes(b"new")
    _insert_capture(conn, (now - timedelta(days=8)).isoformat(), str(old_path))
    _insert_capture(conn, (now - timedelta(days=1)).isoformat(), str(new_path))
    conn.commit()

    assert quota_app._prune_expired(conn, now=now) == 1
    assert conn.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
    assert not old_path.exists()
    assert new_path.exists()
    conn.close()
