from core.lan_server import LanServer


def test_detect_lan_ip_non_loopback():
    ip = LanServer(bridge=None)._detect_lan_ip()
    assert ip and ip != "127.0.0.1"


def test_bind_url_embeds_token():
    srv = LanServer(bridge=None)
    srv.port = 8765
    srv.lan_ip = "192.168.1.5"
    assert srv.bind_url("abc123") == "http://192.168.1.5:8765/?token=abc123"
