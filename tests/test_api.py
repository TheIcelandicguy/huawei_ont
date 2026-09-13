"""Tests for the router API client.

These run without Home Assistant installed — `api.py` only needs `requests`,
and the session is replaced with a scripted fake, so no router is contacted.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from conftest import load_standalone

api = load_standalone("api")

HOST = "router.test"
BASE_URL = f"https://{HOST}"
TOKEN = "deadbeef"
TOKEN_PAGE = f'<input id="onttoken" type="hidden" value="{TOKEN}">'


# --------------------------------------------------------------------------
# scripted session
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class FakeSession:
    """Stands in for requests.Session, answering from a handler function.

    The handler is called as handler(session, method, path, data) and returns
    a FakeResponse; returning None means "404".
    """

    def __init__(self, handler) -> None:
        self.handler = handler
        self.cookies: dict[str, str] = {}
        self.calls: list[SimpleNamespace] = []
        self.verify = True
        self.closed = False

    def _do(self, method: str, url: str, data, kwargs) -> FakeResponse:
        assert url.startswith(BASE_URL), url
        path = url[len(BASE_URL):]
        self.calls.append(
            SimpleNamespace(method=method, path=path, data=data, kwargs=kwargs)
        )
        return self.handler(self, method, path, data) or FakeResponse(404, "")

    def get(self, url, **kwargs):
        return self._do("GET", url, None, kwargs)

    def post(self, url, data=None, **kwargs):
        return self._do("POST", url, data, kwargs)

    def close(self):
        self.closed = True

    def paths(self, method: str | None = None) -> list[str]:
        return [
            c.path for c in self.calls if method is None or c.method == method
        ]


class FakeClock:
    """Deterministic stand-in for the `time` module inside api.py."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


def login_response(session, path, *, ok=True):
    """Answer the two-step login handshake, or None if `path` isn't part of it."""
    if path == api.URL_GET_RAND_COUNT:
        return FakeResponse(200, "﻿1234567890\n")
    if path == api.URL_LOGIN:
        if not ok:
            return FakeResponse(401, "")
        session.cookies["CookieHttps"] = "sessioncookie"
        return FakeResponse(200, "")
    return None


def make_client(handler, clock=None, monkeypatch=None) -> "api.HuaweiOntApi":
    client = api.HuaweiOntApi(HOST, "admin", "secret")
    client._session = FakeSession(handler)
    if clock is not None:
        assert monkeypatch is not None
        monkeypatch.setattr(api, "time", clock)
    return client


# --------------------------------------------------------------------------
# router-shaped fixtures
# --------------------------------------------------------------------------


def device_row(
    host="pc", dtype="Computer", ip="192.168.0.10", mac="AA:BB:CC:DD:EE:FF",
    status="Online", port="LAN1", uptime="3600", recv="100", send="200",
):
    args = [host, dtype, ip, mac, status, port, uptime, "", "", "", recv, send]
    joined = ",".join(f"'{a}'" for a in args)
    return f"new stUserDevInfoPTVDF({joined});"


def wan_ip_row(status="Connected", ip="82.1.2.3", uptime="12345"):
    args = [f"'f{i}'" for i in range(44)]
    args[0] = "'IGD.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1'"
    args[api._WI_CONN_STATUS] = f"'{status}'"
    args[api._WI_IP] = f"'{ip}'"
    args[api._WI_SERVICE] = "'INTERNET_TR069'"
    args[api._WI_UPTIME] = f"'{uptime}'"
    return "new WanIP(" + ",".join(args) + ");"


def wan_stats_row(sent=1000, recv=2000, domain="IGD.WANDevice.1"):
    args = [f"'{domain}.WANConnectionDevice.1.WANIPConnection.1'",
            f"'{sent}'", f"'{recv}'", "'10'", "'20'"]
    return "new WaninfoStats(" + ",".join(args) + ");"


DEVICE_INFO_HTML = """
var cpuUsed = '12%';
var memUsed = '43%';
new stDeviceInfo('IGD.DeviceInfo','SN12345','HW-A','V5R021','V261a-20',
                 'Huawei','x','00:11:22:33:44:55');
"""


# --------------------------------------------------------------------------
# text decoding
# --------------------------------------------------------------------------


def test_decode_hex_leaves_ascii_alone():
    assert api._decode_hex(r"Living\x20Room") == "Living Room"


def test_decode_hex_reassembles_utf8_byte_escapes():
    # "Ásgarður" arrives as the raw UTF-8 bytes, two escapes per accented char
    escaped = r"\xc3\x81sgar\xc3\xb0ur"
    assert api._decode_hex(escaped) == "Ásgarður"


def test_decode_hex_passes_through_real_unicode():
    assert api._decode_hex("Ásgarður") == "Ásgarður"


def test_decode_hex_returns_undecodable_input_untouched():
    # a lone continuation byte is not valid UTF-8; mangling beats nothing
    assert api._decode_hex(r"\xff\xfe") == "\xff\xfe"


def test_unquote_strips_quotes_and_decodes():
    assert api._unquote(r"'caf\xc3\xa9'") == "café"


# --------------------------------------------------------------------------
# javascript-ish parsing
# --------------------------------------------------------------------------


def test_split_constructor_args_respects_quoted_commas():
    assert api._split_constructor_args("'a,b', 'c', 42") == ["'a,b'", "'c'", "42"]


def test_split_constructor_args_respects_nested_parens():
    assert api._split_constructor_args("f(1,2), 'x'") == ["f(1,2)", "'x'"]


def test_parse_constructors_skips_null_and_nested_constructors():
    text = "new Thing('a', null, new Other(1), 'b');"
    assert api._parse_constructors(text, "Thing") == [["a", "b"]]


def test_parse_constructors_finds_every_instance():
    text = "new Row('a');\nnew Row('b');\n"
    assert api._parse_constructors(text, "Row") == [["a"], ["b"]]


def test_parse_single_quoted_vars():
    assert api._parse_single_quoted_vars("var a = 'x'; var b = 'y';") == {
        "a": "x", "b": "y",
    }


def test_is_ipv4():
    assert api._is_ipv4("192.168.0.1")
    assert not api._is_ipv4("fe80::1")
    assert not api._is_ipv4("")


def test_parse_device_info_reads_identity_and_load():
    data = api.RouterData()
    api.HuaweiOntApi(HOST, "u", "p")._parse_device_info(DEVICE_INFO_HTML, data)
    assert data.cpu_usage == 12
    assert data.memory_usage == 43
    assert data.serial_number == "SN12345"
    assert data.model == "V261a-20"
    assert data.mac_address == "00:11:22:33:44:55"


def test_parse_eth_info_reports_speed_only_for_live_ports():
    html = (
        "new GEInfo('IGD.LANDevice.1.LANEthernetInterfaceConfig.LANPort.1.','1','2','1');"
        "new GEInfo('IGD.LANDevice.1.LANEthernetInterfaceConfig.LANPort.2.','1','2','0');"
    )
    data = api.RouterData()
    api.HuaweiOntApi(HOST, "u", "p")._parse_eth_info(html, data)
    assert [(p.port, p.link_up, p.speed) for p in data.lan_ports] == [
        (1, True, "1000M"),
        (2, False, "--"),
    ]


def test_parse_wlan_info_flags_guest_instances():
    html = (
        "new stWlan('IGD.LANDevice.1.WLANConfiguration.1','x','1','MainNet');"
        "new stWlan('IGD.LANDevice.1.WLANConfiguration.3','x','0','GuestNet');"
    )
    data = api.RouterData()
    api.HuaweiOntApi(HOST, "u", "p")._parse_wlan_info(html, data)
    assert [(n.instance, n.ssid, n.enabled, n.is_guest) for n in data.wifi_networks] == [
        (1, "MainNet", True, False),
        (3, "GuestNet", False, True),
    ]


# --------------------------------------------------------------------------
# collapsing the per-IP-family device rows
# --------------------------------------------------------------------------


def parse_devices(html: str) -> api.RouterData:
    data = api.RouterData()
    api.HuaweiOntApi(HOST, "u", "p")._parse_user_devices(html, data)
    return data


def test_online_device_reports_an_online_rows_details_not_a_stale_ones():
    # the same MAC listed twice: a leftover offline IPv4 row and the live IPv6
    # one. Reporting "Online" with the offline row's IP and rates is a lie.
    html = (
        device_row(ip="192.168.0.10", status="Offline", port="LAN1",
                   recv="0", send="0")
        + device_row(ip="fe80::1", status="Online", port="SSID1",
                     recv="500", send="600")
    )
    data = parse_devices(html)
    assert len(data.devices) == 1
    device = data.devices[0]
    assert device.status == "Online"
    assert device.ip_address == "fe80::1"
    assert device.interface == "SSID1"
    assert device.traffic_recv_rate == "500"
    assert data.wifi_client_count == 1
    assert data.lan_client_count == 0


def test_ipv4_wins_among_online_rows():
    html = (
        device_row(ip="fe80::1", status="Online", port="SSID1")
        + device_row(ip="192.168.0.10", status="Online", port="SSID1")
    )
    data = parse_devices(html)
    assert data.devices[0].ip_address == "192.168.0.10"


def test_offline_device_keeps_its_status_and_row():
    html = device_row(ip="192.168.0.10", status="Offline")
    data = parse_devices(html)
    assert data.devices[0].status == "Offline"
    assert data.connected_device_count == 0


def test_hostname_is_taken_from_whichever_row_carries_one():
    html = (
        device_row(host="--", ip="fe80::1", status="Online")
        + device_row(host="Davids-iPhone", ip="192.168.0.11", status="Offline")
    )
    data = parse_devices(html)
    assert data.devices[0].hostname == "Davids-iPhone"


def test_counts_split_wifi_and_lan_clients():
    html = (
        device_row(mac="AA:00:00:00:00:01", port="SSID1")
        + device_row(mac="AA:00:00:00:00:02", port="LAN2")
        + device_row(mac="AA:00:00:00:00:03", port="LAN3", status="Offline")
    )
    data = parse_devices(html)
    assert (data.connected_device_count, data.wifi_client_count,
            data.lan_client_count) == (2, 1, 1)
    assert data.device_list_valid


def test_fresh_router_data_reports_unknown_rather_than_zero_devices():
    data = api.RouterData()
    assert data.device_list_valid is False
    assert data.connected_device_count is None
    assert data.wifi_client_count is None
    assert data.lan_client_count is None


# --------------------------------------------------------------------------
# throughput
# --------------------------------------------------------------------------


def test_compute_rates_handles_the_32_bit_counter_wrapping(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(api, "time", clock)
    client = api.HuaweiOntApi(HOST, "u", "p")

    first = api.RouterData(bytes_sent=api.COUNTER_WRAP - 10_000_000)
    client._compute_rates(first)
    clock.sleep(10)

    second = api.RouterData(bytes_sent=2_500_000)
    client._compute_rates(second)
    # 12.5 MB across the wrap in 10s = 10 Mbit/s, not a negative spike
    assert second.upload_rate_mbps == pytest.approx(10.0)


def test_compute_rates_discards_an_absurd_delta(monkeypatch):
    # a counter reset after a router reboot, not 30 Tbit/s of traffic
    clock = FakeClock()
    monkeypatch.setattr(api, "time", clock)
    client = api.HuaweiOntApi(HOST, "u", "p")

    client._compute_rates(api.RouterData(bytes_received=0))
    clock.sleep(2)
    data = api.RouterData(bytes_received=api.COUNTER_WRAP - 1)
    client._compute_rates(data)
    assert data.download_rate_mbps is None


def test_compute_rates_needs_two_polls(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(api, "time", clock)
    client = api.HuaweiOntApi(HOST, "u", "p")
    data = api.RouterData(bytes_sent=5000, bytes_received=6000)
    client._compute_rates(data)
    assert data.upload_rate_mbps is None
    assert client._last_counters == (clock.now, 5000, 6000)


# --------------------------------------------------------------------------
# session handling on POST
# --------------------------------------------------------------------------


def test_post_never_follows_a_redirect():
    # a followed 302 lands on the login page and returns 200, turning session
    # loss into an ordinary-looking success
    client = make_client(lambda s, m, p, d: login_response(s, p)
                         or FakeResponse(200, "body"))
    client._post("/some/path")
    assert all(
        c.kwargs.get("allow_redirects") is False
        for c in client._session.calls
        if c.path == "/some/path"
    )


@pytest.mark.parametrize("status", [403, 302])
def test_post_re_authenticates_when_the_session_was_evicted(status):
    seen = {"attempts": 0}

    def handler(session, method, path, data):
        login = login_response(session, path)
        if login is not None:
            return login
        seen["attempts"] += 1
        if seen["attempts"] == 1:
            return FakeResponse(status, "")
        return FakeResponse(200, "device list")

    client = make_client(handler)
    client._authenticated = True
    assert client._post("/some/path") == "device list"
    assert seen["attempts"] == 2
    assert client._authenticated
    assert api.URL_LOGIN in client._session.paths()


def test_post_gives_up_when_the_re_login_fails():
    def handler(session, method, path, data):
        login = login_response(session, path, ok=False)
        if login is not None:
            return login
        return FakeResponse(403, "")

    client = make_client(handler)
    client._authenticated = True
    assert client._post("/some/path") is None
    assert client._authenticated is False


def test_a_token_bearing_post_is_not_replayed_after_a_re_login():
    """The onttoken in the payload died with the session, so a replay is a
    guaranteed second 403. Re-authenticate, then hand back to the caller."""
    attempts = {"n": 0}

    def handler(session, method, path, data):
        login = login_response(session, path)
        if login is not None:
            return login
        attempts["n"] += 1
        return FakeResponse(403, "")

    client = make_client(handler)
    client._authenticated = True
    assert client._post("/some/path", "x.X_HW_Token=dead",
                        carries_token=True) is None
    # exactly one attempt: the original. No replay.
    assert attempts["n"] == 1
    # the re-login still happened, so the next caller starts on a live session
    assert api.URL_LOGIN in client._session.paths()
    assert client._authenticated


def test_a_token_bearing_post_does_not_warn_about_a_recoverable_session_loss(
    caplog,
):
    def handler(session, method, path, data):
        return login_response(session, path) or FakeResponse(403, "")

    client = make_client(handler)
    client._authenticated = True
    with caplog.at_level(logging.WARNING):
        client._post("/some/path", "x.X_HW_Token=dead", carries_token=True)
    assert caplog.records == []


def test_a_plain_post_is_still_replayed_after_a_re_login():
    """The contrast with the test above: a payload with no token in it is
    perfectly replayable, and replaying it is what hides the session loss."""
    attempts = {"n": 0}

    def handler(session, method, path, data):
        login = login_response(session, path)
        if login is not None:
            return login
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FakeResponse(403, "")
        return FakeResponse(200, "body")

    client = make_client(handler)
    client._authenticated = True
    assert client._post("/some/path") == "body"
    assert attempts["n"] == 2


def test_successful_login_bumps_the_auth_generation():
    client = make_client(lambda s, m, p, d: login_response(s, p))
    before = client._auth_generation
    assert client.authenticate()
    assert client._auth_generation == before + 1


# --------------------------------------------------------------------------
# the asynchronous device-list rebuild
# --------------------------------------------------------------------------


class BuildScript:
    """Serves the device-list rebuild dance with a scripted state sequence."""

    def __init__(self, states, devices_html=None, clock=None, request_cost=0.0):
        self.states = list(states)
        self.devices_html = devices_html or device_row()
        self.clock = clock
        self.request_cost = request_cost
        self.creating_posts = 0
        self.state_reads = 0
        self.device_reads = 0
        self.token_pages = 0

    def __call__(self, session, method, path, data):
        if self.clock is not None and self.request_cost:
            self.clock.now += self.request_cost
        login = login_response(session, path)
        if login is not None:
            return login
        if path == api.URL_USER_DEV_PAGE:
            self.token_pages += 1
            return FakeResponse(200, TOKEN_PAGE)
        if path == api.URL_USER_DEV_SET_STATE:
            self.creating_posts += 1
            return FakeResponse(200, '"Success"')
        if path == api.URL_USER_DEV_GET_STATE:
            self.state_reads += 1
            state = (
                self.states[self.state_reads - 1]
                if self.state_reads <= len(self.states)
                else self.states[-1]
            )
            return FakeResponse(200, f'"{state}"')
        if path == api.URL_USER_DEVICES:
            self.device_reads += 1
            return FakeResponse(200, self.devices_html)
        return None


def test_a_leftover_completed_is_not_mistaken_for_a_finished_rebuild(monkeypatch):
    clock = FakeClock()
    # first read is the state left behind by the previous poll
    script = BuildScript(["Completed", "Creating", "Completed"])
    client = make_client(script, clock, monkeypatch)
    client._authenticated = True

    assert client._fetch_user_devices() is not None
    # it waited for "Creating" before accepting the second "Completed"
    assert script.state_reads == 3
    assert script.device_reads == 1


def test_a_router_that_never_leaves_completed_is_accepted_after_a_bounded_wait(
    monkeypatch,
):
    clock = FakeClock()
    script = BuildScript(["Completed"])
    client = make_client(script, clock, monkeypatch)
    client._authenticated = True

    assert client._fetch_user_devices() is not None
    assert script.device_reads == 1
    # it did hold out for the transition, but only briefly
    assert clock.slept > api.USER_DEV_BUILD_CONFIRM_TIMEOUT
    assert clock.slept < api.USER_DEV_BUILD_TIMEOUT


def test_a_rebuild_that_never_finishes_yields_no_device_list(monkeypatch):
    clock = FakeClock()
    script = BuildScript(["Creating"])
    client = make_client(script, clock, monkeypatch)
    client._authenticated = True

    assert client._fetch_user_devices() is None
    assert script.device_reads == 0
    assert clock.slept <= api.USER_DEV_BUILD_TIMEOUT


def test_the_build_wait_is_bounded_by_wall_clock_not_by_poll_count(monkeypatch):
    # every request burns 5s, as a hung router's requests do against their
    # timeout. A fixed poll count would let this run for minutes.
    clock = FakeClock()
    script = BuildScript(["Creating"], clock=clock, request_cost=5.0)
    client = make_client(script, clock, monkeypatch)
    client._authenticated = True
    started = clock.now

    assert client._fetch_user_devices() is None
    elapsed = clock.now - started
    assert elapsed < api.USER_DEV_BUILD_TIMEOUT + 3 * api.STATE_POST_TIMEOUT


def test_the_whole_sequence_is_retried_when_a_re_login_voids_the_token(
    monkeypatch,
):
    clock = FakeClock()

    class Script(BuildScript):
        def __call__(self, session, method, path, data):
            if path == api.URL_USER_DEV_SET_STATE and self.creating_posts < 1:
                # the router evicted the session; _post re-logs-in, but the
                # token in `data` belonged to the dead session, so it is not
                # replayed — the caller refetches it and runs the whole
                # sequence again
                self.creating_posts += 1
                return FakeResponse(403, "")
            return super().__call__(session, method, path, data)

    script = Script(["Creating", "Creating", "Completed"])
    client = make_client(script, clock, monkeypatch)
    client._authenticated = True

    assert client._fetch_user_devices() is not None
    # a fresh token was fetched for the second run through
    assert script.token_pages == 2
    assert script.device_reads == 1
    # the dead token was posted once, not twice: one failure, then the retry
    assert client._session.paths("POST").count(api.URL_USER_DEV_SET_STATE) == 2


class ExpiringRouter(BuildScript):
    """A router that checks the onttoken against the live login session.

    The real one ties the token to the session that fetched it and answers
    403 to any token-bearing request carrying a different one. That is what
    made a single expiry cost two failed requests and two logins: the token
    cached from the previous poll is stale for the whole of the next one.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.token = "deadbeef0"
        self.alive = True
        self.logins = 0

    def expire(self) -> None:
        """The router drops the session on its own, as it does every ~140s."""
        self.alive = False

    def __call__(self, session, method, path, data):
        if path == api.URL_GET_RAND_COUNT:
            return FakeResponse(200, "﻿1234567890\n")
        if path == api.URL_LOGIN:
            self.logins += 1
            self.alive = True
            self.token = f"deadbeef{self.logins}"
            session.cookies["CookieHttps"] = "sessioncookie"
            return FakeResponse(200, "")
        if not self.alive:
            return FakeResponse(403, "")
        if data and "x.X_HW_Token=" in data:
            sent = data.split("x.X_HW_Token=")[1].split("&")[0]
            if sent != self.token:
                return FakeResponse(403, "")
        if path == api.URL_USER_DEV_PAGE:
            self.token_pages += 1
            return FakeResponse(
                200,
                f'<input id="onttoken" type="hidden" value="{self.token}">',
            )
        return super().__call__(session, method, path, data)


def test_an_expired_session_costs_one_login_and_one_failed_request(
    monkeypatch, caplog,
):
    """The regression this whole change exists for.

    Before it, one expiry produced four dead requests, two logins and two
    warnings a cycle — about 1,100 log lines a day on a 30s poll — while the
    device list still arrived, so nothing looked broken but the log.
    """
    clock = FakeClock()
    script = ExpiringRouter(["Creating", "Completed"])
    client = make_client(script, clock, monkeypatch)
    client._authenticated = True

    # a healthy poll leaves the token cached
    assert client._get_user_dev_token() == "deadbeef0"
    script.expire()

    with caplog.at_level(logging.WARNING):
        assert client._fetch_user_devices() is not None

    assert script.logins == 1
    assert script.device_reads == 1
    assert caplog.records == []


def test_a_none_body_is_not_a_device_list(monkeypatch):
    clock = FakeClock()
    script = BuildScript(["Creating", "Completed"], devices_html='"NONE"')
    client = make_client(script, clock, monkeypatch)
    client._authenticated = True
    assert client._fetch_user_devices() is None


# --------------------------------------------------------------------------
# the poll as a whole
# --------------------------------------------------------------------------


def full_router(session, method, path, data):
    login = login_response(session, path)
    if login is not None:
        return login
    pages = {
        api.URL_DEVICE_INFO: DEVICE_INFO_HTML,
        api.URL_WAN_CACHE: wan_ip_row(),
        api.URL_WAN_STATS: wan_stats_row(),
        api.URL_ONT_STATE: "var PonMode = 'ge';",
        api.URL_OPTIC_INFO: "",
        api.URL_ETH_INFO: "",
        api.URL_WLAN_BASIC: "",
        api.URL_USER_DEV_PAGE: TOKEN_PAGE,
    }
    if path in pages:
        return FakeResponse(200, pages[path] or " ")
    if path == api.URL_USER_DEV_SET_STATE:
        return FakeResponse(200, '"Success"')
    if path == api.URL_USER_DEV_GET_STATE:
        return FakeResponse(200, '"Completed"')
    if path == api.URL_USER_DEVICES:
        return FakeResponse(200, device_row())
    return None


def test_a_poll_that_returns_nothing_at_all_raises(monkeypatch):
    clock = FakeClock()
    # authenticated fine, but every page 500s — an empty RouterData here would
    # read as a healthy router with nobody connected
    client = make_client(
        lambda s, m, p, d: login_response(s, p) or FakeResponse(500, ""),
        clock, monkeypatch,
    )
    with pytest.raises(api.HuaweiOntConnectionError):
        client.get_router_data()


def test_a_poll_that_cannot_log_in_raises(monkeypatch):
    clock = FakeClock()
    client = make_client(
        lambda s, m, p, d: login_response(s, p, ok=False), clock, monkeypatch
    )
    with pytest.raises(api.HuaweiOntConnectionError):
        client.get_router_data()


def test_a_full_poll_fills_in_every_section(monkeypatch):
    clock = FakeClock()
    client = make_client(full_router, clock, monkeypatch)
    data = client.get_router_data()
    assert data.serial_number == "SN12345"
    assert data.wan_status == "Connected"
    assert data.wan_ip == "82.1.2.3"
    assert data.bytes_sent == 1000
    assert data.ont_state == "ONLINE"
    assert data.device_list_valid
    assert data.connected_device_count == 1


def test_a_failed_device_list_leaves_the_counts_unknown(monkeypatch):
    clock = FakeClock()

    def handler(session, method, path, data):
        if path in (api.URL_USER_DEV_PAGE, api.URL_USER_DEVICES,
                    api.URL_USER_DEV_GET_STATE, api.URL_USER_DEV_SET_STATE):
            return FakeResponse(500, "")
        return full_router(session, method, path, data)

    client = make_client(handler, clock, monkeypatch)
    data = client.get_router_data()
    # the rest of the poll still landed
    assert data.wan_status == "Connected"
    # but nothing may claim the router has no devices
    assert data.device_list_valid is False
    assert data.connected_device_count is None
    assert data.devices == []


def test_a_failed_stats_page_does_not_poison_the_rate_counters(monkeypatch):
    clock = FakeClock()

    def without_stats(session, method, path, data):
        if path == api.URL_WAN_STATS:
            return FakeResponse(500, "")
        return full_router(session, method, path, data)

    client = make_client(without_stats, clock, monkeypatch)
    data = client.get_router_data()
    assert data.bytes_sent == 0
    assert data.download_rate_mbps is None
    # storing zeroes would make the next real reading look like a 4GB spike
    assert client._last_counters is None
