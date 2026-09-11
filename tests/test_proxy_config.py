import logging
import time

from src.proxy_config import get_current_proxy


def test_missing_file_returns_none(tmp_path):
    path = tmp_path / "proxy.txt"
    assert get_current_proxy(path) is None


def test_empty_file_returns_none(tmp_path):
    path = tmp_path / "proxy.txt"
    path.write_text("")
    assert get_current_proxy(path) is None


def test_whitespace_only_file_returns_none(tmp_path):
    path = tmp_path / "proxy.txt"
    path.write_text("   \n\n  ")
    assert get_current_proxy(path) is None


def test_valid_http_proxy_is_returned_stripped(tmp_path):
    path = tmp_path / "proxy.txt"
    path.write_text("  http://127.0.0.1:8080  \n")
    assert get_current_proxy(path) == "http://127.0.0.1:8080"


def test_valid_https_proxy_is_returned_stripped(tmp_path):
    path = tmp_path / "proxy.txt"
    path.write_text("https://user:pass@example.com:443\n")
    assert get_current_proxy(path) == "https://user:pass@example.com:443"


def test_valid_socks5_proxy_is_returned_stripped(tmp_path):
    path = tmp_path / "proxy.txt"
    path.write_text("socks5://127.0.0.1:1080")
    assert get_current_proxy(path) == "socks5://127.0.0.1:1080"


def test_invalid_proxy_value_returns_none_and_logs_warning(tmp_path, caplog):
    path = tmp_path / "proxy.txt"
    path.write_text("not-a-real-proxy-url")

    with caplog.at_level(logging.WARNING):
        result = get_current_proxy(path)

    assert result is None
    assert any(
        record.levelno == logging.WARNING for record in caplog.records
    ) or any("proxy_config" in record.name for record in caplog.records)


def test_content_change_between_calls_returns_new_value(tmp_path):
    path = tmp_path / "proxy.txt"
    path.write_text("http://first-proxy.example.com:8080")

    first = get_current_proxy(path)
    assert first == "http://first-proxy.example.com:8080"

    # Ensure the mtime actually advances - some filesystems have a
    # coarse mtime resolution (e.g. 1s on some setups), so nudge the
    # clock forward a touch to guarantee the cache is invalidated.
    time.sleep(0.01)
    new_mtime = time.time() + 1
    path.write_text("http://second-proxy.example.com:9090")
    import os

    os.utime(path, (new_mtime, new_mtime))

    second = get_current_proxy(path)
    assert second == "http://second-proxy.example.com:9090"
    assert second != first


def test_unchanged_file_is_not_reread_from_disk(tmp_path, monkeypatch):
    path = tmp_path / "proxy.txt"
    path.write_text("http://cached-proxy.example.com:8080")

    first = get_current_proxy(path)
    assert first == "http://cached-proxy.example.com:8080"

    original_read_text = type(path).read_text
    calls = {"count": 0}

    def _counting_read_text(self, *args, **kwargs):
        calls["count"] += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", _counting_read_text)

    second = get_current_proxy(path)
    assert second == first
    assert calls["count"] == 0
