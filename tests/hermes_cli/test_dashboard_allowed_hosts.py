from hermes_cli import web_server


def test_dashboard_allowed_hosts_env_keeps_loopback_rebinding_guard(monkeypatch):
    monkeypatch.setenv(
        "HERMES_DASHBOARD_ALLOWED_HOSTS",
        "hermes.bertramopenclaw.com,100.107.89.62,srv1420950",
    )

    assert web_server._is_accepted_host("hermes.bertramopenclaw.com", "127.0.0.1")
    assert web_server._is_accepted_host("100.107.89.62:9119", "127.0.0.1")
    assert web_server._is_accepted_host("srv1420950:9119", "127.0.0.1")
    assert web_server._is_accepted_host("127.0.0.1:9119", "127.0.0.1")
    assert not web_server._is_accepted_host("evil.example", "127.0.0.1")
