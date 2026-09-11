import json

import scripts.health_monitor as health_monitor


def _point_status_paths_at(monkeypatch, status_dir):
    """Redirect the module's status-file paths at a tmp_path directory
    so tests never touch the real project's status/ folder - same
    override pattern src/proxy_config.py's own tests use."""
    monkeypatch.setattr(health_monitor, "_STATUS_DIR", status_dir)
    monkeypatch.setattr(health_monitor, "_STATUS_JSON_PATH", status_dir / "status.json")
    monkeypatch.setattr(health_monitor, "_STATUS_HTML_PATH", status_dir / "status.html")


def test_write_status_ok_writes_green_html_and_json(tmp_path, monkeypatch):
    status_dir = tmp_path / "status"
    _point_status_paths_at(monkeypatch, status_dir)

    health_monitor.write_status(ok=True, proxy_host="proxy.example.com", error=None)

    payload = json.loads((status_dir / "status.json").read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["proxy_host"] == "proxy.example.com"
    assert payload["error"] is None
    assert "checked_at" in payload

    html = (status_dir / "status.html").read_text(encoding="utf-8")
    assert "#2ecc71" in html
    assert "proxy.example.com" in html


def test_write_status_failure_writes_red_html_and_json(tmp_path, monkeypatch):
    status_dir = tmp_path / "status"
    _point_status_paths_at(monkeypatch, status_dir)

    health_monitor.write_status(ok=False, proxy_host=None, error="Connection timed out")

    payload = json.loads((status_dir / "status.json").read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["proxy_host"] is None
    assert payload["error"] == "Connection timed out"

    html = (status_dir / "status.html").read_text(encoding="utf-8")
    assert "#e74c3c" in html
    assert "Connection timed out" in html


def test_write_status_creates_status_dir_if_missing(tmp_path, monkeypatch):
    status_dir = tmp_path / "nested" / "status"
    _point_status_paths_at(monkeypatch, status_dir)

    assert not status_dir.exists()
    health_monitor.write_status(ok=True, proxy_host=None, error=None)

    assert status_dir.exists()
    assert (status_dir / "status.json").exists()
    assert (status_dir / "status.html").exists()


def test_html_has_ten_second_refresh_meta_tag(tmp_path, monkeypatch):
    status_dir = tmp_path / "status"
    _point_status_paths_at(monkeypatch, status_dir)

    health_monitor.write_status(ok=True, proxy_host=None, error=None)

    html = (status_dir / "status.html").read_text(encoding="utf-8")
    assert '<meta http-equiv="refresh" content="10">' in html


def test_write_status_no_error_line_when_ok_even_if_error_passed(tmp_path, monkeypatch):
    # error should only ever be shown when ok is False - a stale error
    # string passed alongside ok=True should not leak into the page.
    status_dir = tmp_path / "status"
    _point_status_paths_at(monkeypatch, status_dir)

    health_monitor.write_status(ok=True, proxy_host=None, error="stale error")

    html = (status_dir / "status.html").read_text(encoding="utf-8")
    assert "stale error" not in html


def test_proxy_host_strips_credentials_and_scheme():
    assert (
        health_monitor._proxy_host("https://user:pass@example.com:8080")
        == "example.com"
    )


def test_proxy_host_none_when_no_proxy():
    assert health_monitor._proxy_host(None) is None


def test_proxy_host_plain_http_proxy():
    assert health_monitor._proxy_host("http://10.0.0.5:3128") == "10.0.0.5"
