"""API client for Huawei OptiXstar ONT routers.

Parses data from the router's web UI which outputs JavaScript constructor
patterns (new ClassName(...)) and single-quoted variables, not JSON.
"""

import base64
import logging
import re
import time
from dataclasses import dataclass, field

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_LOGGER = logging.getLogger(__name__)

URL_GET_RAND_COUNT = "/asp/GetRandCount.asp"
URL_LOGIN = "/login.cgi"
URL_DEVICE_INFO = "/html/ssmp/deviceinfo/deviceinfo.asp"
URL_WAN_CACHE = "/html/bbsp/common/wan_list_cache_wan.asp"
URL_WAN_STATS = "/html/bbsp/common/get_wan_list_ipwanstat.asp"
URL_ONT_STATE = "/html/bbsp/common/ontstate.asp"
URL_USER_DEVICES = "/html/bbsp/userdevinfo/getuserdevinfo.asp"
URL_USER_DEV_PAGE = "/html/bbsp/userdevinfo/userdevinfo1.asp"
USER_DEV_DOMAIN = (
    "InternetGatewayDevice.X_HW_FeatureList.BBSPCustomization.UserDevInfo"
)
URL_USER_DEV_GET_STATE = (
    f"/getajax.cgi?x={USER_DEV_DOMAIN}&RequestFile=nopage"
)
URL_USER_DEV_SET_STATE = (
    f"/html/bbsp/userdevinfo/setajax.cgi?x={USER_DEV_DOMAIN}&RequestFile=nopage"
)
# The router rebuilds the device list asynchronously and reports "Completed"
# after ~2s. Poll often enough to catch the state *leaving* "Completed", since
# that transition is the only proof the rebuild we asked for actually started,
# and bound the whole wait by wall clock so a hung router cannot stall a poll
# for longer than the scan interval.
USER_DEV_BUILD_POLL_INTERVAL = 0.25
USER_DEV_BUILD_TIMEOUT = 12.0
# how long to insist on that transition before accepting a "Completed" that was
# already there — a router that rebuilds faster than we poll must not hang here
USER_DEV_BUILD_CONFIRM_TIMEOUT = 2.0
URL_OPTIC_INFO = "/html/amp/opticinfo/opticinfo.asp"
URL_ETH_INFO = "/html/amp/ethinfo/ethinfo.asp"
URL_WLAN_BASIC = "/html/amp/wlanbasic/WlanBasic.asp?2G"
URL_WLAN_SET_CGI = "/html/amp/wlanbasic/set.cgi"
URL_CFGFILE = "/html/ssmp/cfgfile/cfgfileroot.asp"
URL_REBOOT_CGI = (
    "/html/ssmp/cfgfile/set.cgi"
    "?x=InternetGatewayDevice.X_HW_DEBUG.SSP.DBSave"
    "&y=InternetGatewayDevice.X_HW_DEBUG.SMP.DM.ResetBoard"
    "&RequestFile=html/ssmp/cfgfile/cfgfileroot.asp"
)

RE_ONT_TOKEN = re.compile(
    r'(?:id|name)="onttoken"[^>]*value="([0-9a-fA-F]+)"'
)
RE_WLAN_INSTANCE = re.compile(r'WLANConfiguration\.(\d+)$')

# WLANConfiguration instances 3/4 (2.4G) and 7/8 (5G) are guest networks
GUEST_WLAN_INSTANCES = {3, 4, 7, 8}

# GEInfo Speed field -> link speed label
LAN_SPEED_MAP = {
    "0": "10M",
    "1": "100M",
    "2": "1000M",
    "3": "2.5G",
    "4": "10G",
    "6": "5G",
    "7": "25G",
}
COUNTER_WRAP = 2**32

RE_SINGLE_QUOTED_VAR = re.compile(r"var\s+(\w+)\s*=\s*'([^']*)'\s*;")
RE_HEX_ESCAPE = re.compile(r'\\x([0-9a-fA-F]{2})')

# per-request timeouts: the device-list body is large and slow to render, the
# little state polls are not and must not hold a poll open
POST_TIMEOUT = 25
STATE_POST_TIMEOUT = 10


class HuaweiOntError(Exception):
    """Base error for this integration."""


class HuaweiOntConnectionError(HuaweiOntError):
    """The router returned nothing at all — unreachable, or the login failed."""


def _decode_hex(s: str) -> str:
    r"""Expand the router's \xNN escapes.

    The escapes carry raw UTF-8 *bytes*, not code points, so decoding them one
    at a time leaves anything above ASCII as mojibake — an SSID of "Ásgarður"
    arrives as six escapes and would come out "ÃsgarÃ°ur". Round the result
    back through latin-1 to recover the original bytes and decode them as the
    UTF-8 they are. ASCII is unaffected, and text that is already proper
    Unicode (or is malformed) is returned untouched rather than mangled.
    """
    out = RE_HEX_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), s)
    if out.isascii():
        return out
    try:
        return out.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return out


def _is_ipv4(addr: str) -> bool:
    return ":" not in addr and addr.count(".") == 3


def _split_constructor_args(args_str: str) -> list[str]:
    """Split comma-separated constructor arguments, respecting quoted strings."""
    args = []
    current = []
    in_quote = None
    depth = 0
    i = 0
    while i < len(args_str):
        c = args_str[i]
        if c == '\\' and i + 1 < len(args_str):
            current.append(c)
            current.append(args_str[i + 1])
            i += 2
            continue
        if in_quote:
            if c == in_quote:
                in_quote = None
            current.append(c)
        elif c in ('"', "'"):
            in_quote = c
            current.append(c)
        elif c == '(':
            depth += 1
            current.append(c)
        elif c == ')':
            depth -= 1
            current.append(c)
        elif c == ',' and depth == 0:
            args.append(''.join(current).strip())
            current = []
        else:
            current.append(c)
        i += 1
    if current:
        args.append(''.join(current).strip())
    return args


def _unquote(s: str) -> str:
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        s = s[1:-1]
    return _decode_hex(s)


def _find_constructor_args(text: str, start: int) -> str | None:
    """From the opening '(' at `start`, find the matching ')' respecting
    nesting and quoted strings. Returns the content between parens."""
    if start >= len(text) or text[start] != '(':
        return None
    depth = 0
    in_quote = None
    i = start
    while i < len(text):
        c = text[i]
        if c == '\\' and i + 1 < len(text) and in_quote:
            i += 2
            continue
        if in_quote:
            if c == in_quote:
                in_quote = None
        elif c in ('"', "'"):
            in_quote = c
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
        i += 1
    return None


def _parse_constructors(text: str, class_name: str) -> list[list[str]]:
    """Extract all instances of `new ClassName(arg1, arg2, ...)` and return
    a list of decoded argument lists."""
    results = []
    pattern = f'new {class_name}('
    search_from = 0
    while True:
        idx = text.find(pattern, search_from)
        if idx < 0:
            break
        paren_start = idx + len(pattern) - 1
        args_str = _find_constructor_args(text, paren_start)
        if args_str is None:
            search_from = idx + 1
            continue
        raw_args = _split_constructor_args(args_str)
        decoded = []
        for arg in raw_args:
            stripped = arg.strip()
            if stripped == 'null':
                continue
            if stripped.startswith('new '):
                continue
            decoded.append(_unquote(stripped))
        if decoded:
            results.append(decoded)
        search_from = paren_start + len(args_str) + 1
    return results


def _parse_single_quoted_vars(text: str) -> dict[str, str]:
    result = {}
    for m in RE_SINGLE_QUOTED_VAR.finditer(text):
        result[m.group(1)] = _decode_hex(m.group(2))
    return result


@dataclass
class ConnectedDevice:
    hostname: str
    ip_address: str
    mac_address: str
    status: str
    interface: str
    device_type: str
    online_duration: str
    traffic_recv_rate: str = "0"
    traffic_send_rate: str = "0"


@dataclass
class LanPort:
    port: int
    link_up: bool
    speed: str = "--"
    duplex: str = "--"


@dataclass
class WifiNetwork:
    instance: int
    band: str
    ssid: str
    enabled: bool
    is_guest: bool
    channel: str = ""
    standard: str = ""


@dataclass
class RouterData:
    cpu_usage: int = 0
    memory_usage: int = 0
    ont_state: str = "UNKNOWN"
    wan_status: str = "Disconnected"
    wan_ip: str = ""
    wan_uptime: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    packets_sent: int = 0
    packets_received: int = 0
    # None until a device list is actually parsed: a failed fetch must show up
    # as "unknown" on the sensors, not as a router with nobody connected
    connected_device_count: int | None = None
    wifi_client_count: int | None = None
    lan_client_count: int | None = None
    download_rate_mbps: float | None = None
    upload_rate_mbps: float | None = None
    optical_temperature: float | None = None
    optical_voltage: float | None = None
    optical_tx_power: str = "--"
    optical_rx_power: str = "--"
    model: str = "V261a-20"
    serial_number: str = ""
    software_version: str = ""
    hardware_version: str = ""
    mac_address: str = ""
    # whether `devices` came from the router this poll. An empty list means
    # "nobody is connected" only when this is True; otherwise it means the
    # fetch failed and consumers must leave their existing state alone.
    device_list_valid: bool = False
    devices: list[ConnectedDevice] = field(default_factory=list)
    lan_ports: list[LanPort] = field(default_factory=list)
    wifi_networks: list[WifiNetwork] = field(default_factory=list)


# stDeviceInfo positional arg indices
_DI_SERIAL = 1
_DI_HW_VER = 2
_DI_SW_VER = 3
_DI_MODEL = 4
_DI_MAC = 7

# WanIP positional arg indices
_WI_STATUS = 5
_WI_CONN_STATUS = 12
_WI_IP = 15
_WI_SERVICE = 26
_WI_UPTIME = 43

# WaninfoStats positional arg indices
_WS_DOMAIN = 0
_WS_BYTES_SENT = 1
_WS_BYTES_RECV = 2
_WS_PKTS_SENT = 3
_WS_PKTS_RECV = 4

# stUserDevInfoPTVDF positional arg indices
_UD_HOST = 0
_UD_DEVTYPE = 1
_UD_IP = 2
_UD_MAC = 3
_UD_STATUS = 4
_UD_PORT = 5
_UD_TIME = 6
_UD_RECV_RATE = 10
_UD_SEND_RATE = 11

# stOpticInfo positional arg indices
_OI_LINK_STATUS = 1
_OI_TX_POWER = 2
_OI_RX_POWER = 3
_OI_VOLTAGE = 4
_OI_TEMPERATURE = 5

# GEInfo positional arg indices (domain, Mode, Speed, Status, OptEthMode)
_GE_DOMAIN = 0
_GE_MODE = 1
_GE_SPEED = 2
_GE_STATUS = 3


class HuaweiOntApi:
    """API client for Huawei OptiXstar ONT."""

    def __init__(self, host: str, username: str, password: str) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._base_url = f"https://{host}"
        self._session: requests.Session | None = None
        self._authenticated = False
        # (monotonic timestamp, bytes_sent, bytes_received) from last poll,
        # used to derive throughput rates
        self._last_counters: tuple[float, int, int] | None = None
        # rapid login retries trigger the router's anti-brute-force lockout,
        # so back off after a failed login
        self._auth_blocked_until: float = 0.0
        # onttoken from the user-device page, reused across polls
        self._user_dev_token: str | None = None
        # bumped on every successful login, so a caller mid-way through a
        # token-bearing sequence can tell that the session moved under it
        self._auth_generation = 0

    def _ensure_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.verify = False
            self._authenticated = False
        return self._session

    def authenticate(self) -> bool:
        if time.monotonic() < self._auth_blocked_until:
            _LOGGER.debug("Skipping login attempt during backoff window")
            return False
        session = self._ensure_session()
        # tokens are tied to the old session, so they never survive a login
        self._user_dev_token = None
        try:
            r = session.post(f"{self._base_url}{URL_GET_RAND_COUNT}", timeout=10)
            token = r.text.strip().strip('﻿')

            pwd_b64 = base64.b64encode(self._password.encode()).decode()
            r = session.post(
                f"{self._base_url}{URL_LOGIN}",
                data={
                    "UserName": self._username,
                    "PassWord": pwd_b64,
                    "Language": "english",
                    "x.X_HW_Token": token,
                },
                allow_redirects=False,
                timeout=10,
            )

            if r.status_code == 200 and "CookieHttps" in session.cookies:
                self._authenticated = True
                self._auth_generation += 1
                _LOGGER.debug("Authentication successful")
                return True

            _LOGGER.error("Login failed: status %d", r.status_code)
            self._authenticated = False
            self._auth_blocked_until = time.monotonic() + 60
            return False

        except Exception as err:
            _LOGGER.error("Authentication error: %s", err)
            self._authenticated = False
            self._auth_blocked_until = time.monotonic() + 60
            return False

    def _fetch_page(self, path: str) -> str | None:
        session = self._ensure_session()
        if not self._authenticated:
            if not self.authenticate():
                return None
        try:
            r = session.get(f"{self._base_url}{path}", timeout=15)
            if r.status_code == 200:
                return r.text
            if r.status_code in (403, 302):
                _LOGGER.debug("Session expired, re-authenticating")
                self._authenticated = False
                if self.authenticate():
                    r = session.get(f"{self._base_url}{path}", timeout=15)
                    if r.status_code == 200:
                        return r.text
            _LOGGER.warning("Failed to fetch %s: status %d", path, r.status_code)
            return None
        except Exception as err:
            _LOGGER.error("Error fetching %s: %s", path, err)
            return None

    def _post(
        self,
        path: str,
        data: str | None = None,
        timeout: int = POST_TIMEOUT,
        carries_token: bool = False,
    ) -> str | None:
        """POST to a path on an authenticated session, returning the body.

        Handles an evicted session the same way _fetch_page does. The router
        answers a request on a dead session with a redirect to the login page,
        so redirects must stay unfollowed — following one turns session loss
        into a perfectly ordinary 200 carrying the login form, and the client
        would sit there logged out until Home Assistant restarts.

        `carries_token` marks a payload with an onttoken baked into it. Such a
        payload cannot be replayed after a re-login: the token belonged to the
        session that just died, so the replay is a guaranteed second 403 and
        the only thing it adds is a warning in the log. Those callers get None
        and are expected to drop the cached token and run their sequence
        again on the fresh session.
        """
        session = self._ensure_session()
        if not self._authenticated:
            if not self.authenticate():
                return None
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        url = f"{self._base_url}{path}"
        try:
            r = session.post(
                url, data=data, headers=headers, timeout=timeout,
                allow_redirects=False,
            )
            if r.status_code == 200:
                return r.text
            if r.status_code in (403, 302):
                _LOGGER.debug("Session expired, re-authenticating")
                self._authenticated = False
                if self.authenticate():
                    if carries_token:
                        _LOGGER.debug(
                            "Not replaying %s: its onttoken died with the "
                            "old session",
                            path,
                        )
                        return None
                    r = session.post(
                        url, data=data, headers=headers, timeout=timeout,
                        allow_redirects=False,
                    )
                    if r.status_code == 200:
                        return r.text
            _LOGGER.warning("Failed to POST %s: status %d", path, r.status_code)
            return None
        except Exception as err:
            _LOGGER.error("Error posting to %s: %s", path, err)
            return None

    def _get_user_dev_token(self) -> str | None:
        """Fetch (and cache) the onttoken the device-list endpoints require."""
        if self._user_dev_token:
            return self._user_dev_token
        html = self._fetch_page(URL_USER_DEV_PAGE)
        if not html:
            return None
        m = RE_ONT_TOKEN.search(html)
        if not m:
            _LOGGER.warning("onttoken not found on the user-device page")
            return None
        self._user_dev_token = m.group(1)
        return self._user_dev_token

    def _read_build_state(self, token: str) -> str | None:
        state = self._post(
            URL_USER_DEV_GET_STATE,
            f"State&x.X_HW_Token={token}",
            timeout=STATE_POST_TIMEOUT,
            carries_token=True,
        )
        return _decode_hex(state) if state is not None else None

    def _fetch_user_devices(self) -> str | None:
        """Fetch the connected-device list.

        The onttoken these endpoints take belongs to the login session, so a
        re-login part-way through the sequence invalidates the one in flight.
        Notice that and run the whole thing once more on the fresh session
        rather than losing a poll's worth of devices.
        """
        generation = self._auth_generation
        html = self._fetch_user_devices_once()
        if html is None and self._auth_generation != generation:
            _LOGGER.debug("Session was replaced mid-fetch, retrying device list")
            html = self._fetch_user_devices_once()
        return html

    def _fetch_user_devices_once(self) -> str | None:
        """Ask the router to rebuild the device list, then read it.

        The router does not serve this list live. It renders it into a file
        only when asked, so a plain read returns either "NONE" (never built)
        or a stale snapshot from whenever it was last generated.
        """
        generation = self._auth_generation
        token = self._get_user_dev_token()
        if not token:
            return None

        # The state is left at "Completed" by the previous poll, so seeing it
        # again proves nothing about the rebuild we are about to request. Note
        # that up front and hold out for the state to change.
        before = self._read_build_state(token)
        if before is None and self._auth_generation != generation:
            # The session was replaced under us, which killed the token we are
            # holding. Posting the rest of the sequence with it would only
            # collect another 403 and another login; hand back to the caller,
            # which refetches the token and runs the whole thing again.
            self._user_dev_token = None
            return None
        stale = before is not None and "Completed" in before

        if self._post(
            URL_USER_DEV_SET_STATE,
            f"x.State=Creating&x.X_HW_Token={token}",
            timeout=STATE_POST_TIMEOUT,
            carries_token=True,
        ) is None:
            # a rejected token usually means the session was replaced
            self._user_dev_token = None
            return None

        started = time.monotonic()
        deadline = started + USER_DEV_BUILD_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(USER_DEV_BUILD_POLL_INTERVAL)
            state = self._read_build_state(token)
            if state is None:
                continue
            if "Completed" not in state:
                stale = False  # the rebuild we asked for is under way
                continue
            if not stale:
                break
            if time.monotonic() - started > USER_DEV_BUILD_CONFIRM_TIMEOUT:
                _LOGGER.debug(
                    "Router never left 'Completed'; the device list may be "
                    "the snapshot from the previous rebuild"
                )
                break
        else:
            _LOGGER.warning(
                "Router did not finish building the device list in %.0fs",
                USER_DEV_BUILD_TIMEOUT,
            )
            return None

        html = self._post(URL_USER_DEVICES)
        if html is None or html.strip().strip('"') == "NONE":
            return None
        return html

    def _parse_device_info(self, html: str, data: RouterData) -> None:
        variables = _parse_single_quoted_vars(html)

        cpu = variables.get("cpuUsed", "0%").replace("%", "").strip()
        try:
            data.cpu_usage = int(cpu)
        except (ValueError, TypeError):
            pass

        mem = variables.get("memUsed", "0%").replace("%", "").strip()
        try:
            data.memory_usage = int(mem)
        except (ValueError, TypeError):
            pass

        devices = _parse_constructors(html, "stDeviceInfo")
        if devices:
            args = devices[0]
            if len(args) > _DI_MAC:
                data.serial_number = args[_DI_SERIAL]
                data.hardware_version = args[_DI_HW_VER]
                data.software_version = args[_DI_SW_VER]
                data.model = args[_DI_MODEL]
                data.mac_address = args[_DI_MAC]

    def _parse_wan_cache(self, html: str, data: RouterData) -> str:
        """Parse WAN connection list. Returns the domain path of the internet
        WAN connection for matching with stats."""
        wan_domain = ""
        entries = _parse_constructors(html, "WanIP")
        for args in entries:
            if len(args) <= _WI_UPTIME:
                continue
            service = args[_WI_SERVICE] if len(args) > _WI_SERVICE else ""
            if "INTERNET" not in service:
                continue
            data.wan_status = args[_WI_CONN_STATUS]
            data.wan_ip = args[_WI_IP]
            wan_domain = args[0]
            try:
                data.wan_uptime = int(args[_WI_UPTIME])
            except (ValueError, TypeError):
                pass
            break
        return wan_domain

    def _parse_wan_stats(self, html: str, data: RouterData, wan_domain: str) -> None:
        entries = _parse_constructors(html, "WaninfoStats")
        for args in entries:
            if len(args) <= _WS_PKTS_RECV:
                continue
            domain = args[_WS_DOMAIN]
            if wan_domain and not domain.startswith(wan_domain):
                continue
            try:
                data.bytes_sent = int(args[_WS_BYTES_SENT])
                data.bytes_received = int(args[_WS_BYTES_RECV])
                data.packets_sent = int(args[_WS_PKTS_SENT])
                data.packets_received = int(args[_WS_PKTS_RECV])
            except (ValueError, TypeError):
                pass
            break

    def _parse_ont_state(self, html: str, data: RouterData) -> None:
        variables = _parse_single_quoted_vars(html)
        pon_mode = variables.get("PonMode", "").lower()
        if pon_mode == "ge":
            # GE terminal: derive ONT state from WAN status
            data.ont_state = (
                "ONLINE" if data.wan_status == "Connected" else "OFFLINE"
            )
        else:
            # GPON/EPON: check the constructor data
            if pon_mode == "gpon":
                entries = _parse_constructors(html, "OntStateInfo")
            else:
                entries = _parse_constructors(html, "OntStateInfo")
            if entries:
                status = entries[0][2] if len(entries[0]) > 2 else ""
                if status.upper() in ("O5", "O5AUTH"):
                    data.ont_state = "ONLINE"
                else:
                    data.ont_state = "OFFLINE"

    def _parse_user_devices(self, html: str, data: RouterData) -> None:
        entries = _parse_constructors(html, "stUserDevInfoPTVDF")

        # The router emits one row per IP family, so a device holding an IPv6
        # link-local address is listed twice under the same MAC. Collapse the
        # rows per MAC and keep the IPv4 one, otherwise the counts run high
        # and a device can end up reporting its fe80:: address.
        by_mac: dict[str, list[list[str]]] = {}
        for args in entries:
            if len(args) < 7:
                continue
            by_mac.setdefault(args[_UD_MAC].lower(), []).append(args)

        devices = []
        for rows in by_mac.values():
            online = [r for r in rows if r[_UD_STATUS] == "Online"]
            # Online on any row means the device is present, so the row we
            # report has to be an online one — an offline leftover carries the
            # IP, interface and rates from whenever the device was last seen,
            # and pairing those with an "Online" status is simply a lie.
            pool = online or rows
            args = next((r for r in pool if _is_ipv4(r[_UD_IP])), pool[0])
            # a duplicate row may carry the hostname when the chosen one lacks it
            hostname = next(
                (r[_UD_HOST] for r in (args, *rows) if r[_UD_HOST] not in ("--", "")),
                "",
            )
            devices.append(ConnectedDevice(
                hostname=hostname,
                ip_address=args[_UD_IP],
                mac_address=args[_UD_MAC],
                status="Online" if online else args[_UD_STATUS],
                interface=args[_UD_PORT],
                device_type=args[_UD_DEVTYPE],
                online_duration=args[_UD_TIME],
                traffic_recv_rate=args[_UD_RECV_RATE] if len(args) > _UD_RECV_RATE else "0",
                traffic_send_rate=args[_UD_SEND_RATE] if len(args) > _UD_SEND_RATE else "0",
            ))

        data.devices = devices
        data.device_list_valid = True
        data.connected_device_count = sum(
            1 for d in devices if d.status == "Online"
        )
        data.wifi_client_count = sum(
            1 for d in devices
            if d.status == "Online" and d.interface.upper().startswith("SSID")
        )
        data.lan_client_count = sum(
            1 for d in devices
            if d.status == "Online" and d.interface.upper().startswith("LAN")
        )

    def _parse_optic_info(self, html: str, data: RouterData) -> None:
        entries = _parse_constructors(html, "stOpticInfo")
        for args in entries:
            if len(args) <= _OI_TEMPERATURE:
                continue
            try:
                data.optical_voltage = float(args[_OI_VOLTAGE])
            except (ValueError, TypeError):
                pass
            try:
                data.optical_temperature = float(args[_OI_TEMPERATURE])
            except (ValueError, TypeError):
                pass
            data.optical_tx_power = args[_OI_TX_POWER]
            data.optical_rx_power = args[_OI_RX_POWER]
            break

    def _parse_eth_info(self, html: str, data: RouterData) -> None:
        entries = _parse_constructors(html, "GEInfo")
        ports = []
        for args in entries:
            if len(args) <= _GE_STATUS:
                continue
            m = re.search(r'LANPort\.(\d+)\.', args[_GE_DOMAIN])
            if not m:
                continue
            port_num = int(m.group(1))
            link_up = args[_GE_STATUS] == "1"
            ports.append(LanPort(
                port=port_num,
                link_up=link_up,
                speed=LAN_SPEED_MAP.get(args[_GE_SPEED], "--") if link_up else "--",
                duplex=("Full" if args[_GE_MODE] == "1" else "Half") if link_up else "--",
            ))
        data.lan_ports = sorted(ports, key=lambda p: p.port)

    def _parse_wlan_info(self, html: str, data: RouterData) -> None:
        # actual channel per WLAN instance (stWlanWifi reports 0 = auto)
        channels: dict[str, str] = {}
        for args in _parse_constructors(html, "getChannels"):
            if len(args) >= 2:
                channels[args[0]] = args[1]

        # wifi standard per instance (e.g. 11be)
        standards: dict[str, str] = {}
        for args in _parse_constructors(html, "stWlanWifi"):
            if len(args) >= 5 and args[0]:
                standards[args[0]] = args[4]

        # stWlan covers every existing WLANConfiguration: main + guest
        networks = []
        for args in _parse_constructors(html, "stWlan"):
            if len(args) < 4 or not args[0]:
                continue
            m = RE_WLAN_INSTANCE.search(args[0])
            if not m:
                continue
            instance = int(m.group(1))
            networks.append(WifiNetwork(
                instance=instance,
                band="2.4G" if instance <= 4 else "5G",
                ssid=args[3],
                enabled=args[2] == "1",
                is_guest=instance in GUEST_WLAN_INSTANCES,
                channel=channels.get(args[0], ""),
                standard=standards.get(args[0], ""),
            ))
        data.wifi_networks = sorted(networks, key=lambda n: n.instance)

    def set_wifi_enabled(self, instance: int, enabled: bool) -> bool:
        """Enable or disable a WLANConfiguration instance (main or guest)."""
        wlan_html = self._fetch_page(URL_WLAN_BASIC)
        if not wlan_html:
            _LOGGER.error("WiFi toggle failed: could not load WLAN page")
            return False
        m = RE_ONT_TOKEN.search(wlan_html)
        if not m:
            _LOGGER.error("WiFi toggle failed: onttoken not found")
            return False
        domain = (
            f"InternetGatewayDevice.LANDevice.1.WLANConfiguration.{instance}"
        )
        session = self._ensure_session()
        try:
            r = session.post(
                f"{self._base_url}{URL_WLAN_SET_CGI}"
                f"?x={domain}&RequestFile=html/amp/wlanbasic/WlanBasic.asp",
                data={
                    "x.Enable": "1" if enabled else "0",
                    "x.SSIDAdvertisementEnabled": "1",
                    "x.X_HW_Token": m.group(1),
                },
                timeout=20,
                allow_redirects=False,
            )
            ok = r.status_code == 200
            _LOGGER.info(
                "WiFi instance %d set enabled=%s: status %d",
                instance, enabled, r.status_code,
            )
            return ok
        except Exception as err:
            _LOGGER.error("WiFi toggle error: %s", err)
            return False

    def _compute_rates(self, data: RouterData) -> None:
        """Derive throughput in Mbit/s from WAN byte counter deltas."""
        now = time.monotonic()
        if self._last_counters is not None:
            last_ts, last_sent, last_recv = self._last_counters
            dt = now - last_ts
            if dt > 1:
                d_sent = data.bytes_sent - last_sent
                d_recv = data.bytes_received - last_recv
                # counters are 32-bit and wrap
                if d_sent < 0:
                    d_sent += COUNTER_WRAP
                if d_recv < 0:
                    d_recv += COUNTER_WRAP
                up = d_sent * 8 / dt / 1_000_000
                down = d_recv * 8 / dt / 1_000_000
                # discard absurd values (counter reset after router reboot)
                if up < 10_000 and down < 10_000:
                    data.upload_rate_mbps = round(up, 2)
                    data.download_rate_mbps = round(down, 2)
        self._last_counters = (now, data.bytes_sent, data.bytes_received)

    def get_router_data(self) -> RouterData:
        """Poll every page the entities read.

        One page failing is survivable — the router drops one now and then and
        the affected values simply stay at their defaults. A poll where
        *nothing* came back is a different animal: an empty RouterData reads as
        a perfectly healthy router with no traffic and nobody connected, which
        zeroes the sensors and sends every tracker away. Raise instead, so the
        coordinator marks the integration unavailable and the last known state
        stands.
        """
        data = RouterData()
        fetched = 0

        device_html = self._fetch_page(URL_DEVICE_INFO)
        if device_html:
            fetched += 1
            self._parse_device_info(device_html, data)

        wan_domain = ""
        wan_cache_html = self._fetch_page(URL_WAN_CACHE)
        if wan_cache_html:
            fetched += 1
            wan_domain = self._parse_wan_cache(wan_cache_html, data)

        wan_stats_html = self._fetch_page(URL_WAN_STATS)
        if wan_stats_html:
            fetched += 1
            self._parse_wan_stats(wan_stats_html, data, wan_domain)

        ont_html = self._fetch_page(URL_ONT_STATE)
        if ont_html:
            fetched += 1
            self._parse_ont_state(ont_html, data)

        devices_html = self._fetch_user_devices()
        if devices_html:
            fetched += 1
            self._parse_user_devices(devices_html, data)

        optic_html = self._fetch_page(URL_OPTIC_INFO)
        if optic_html:
            fetched += 1
            self._parse_optic_info(optic_html, data)

        eth_html = self._fetch_page(URL_ETH_INFO)
        if eth_html:
            fetched += 1
            self._parse_eth_info(eth_html, data)

        wlan_html = self._fetch_page(URL_WLAN_BASIC)
        if wlan_html:
            fetched += 1
            self._parse_wlan_info(wlan_html, data)

        if not fetched:
            raise HuaweiOntConnectionError(
                f"No data returned by the router at {self._host}"
            )

        # Only feed the rate calculation counters that actually came from the
        # router: storing the default zeroes would make the next real reading
        # look like a 4 GB spike and cost two polls' worth of rates.
        if wan_stats_html:
            self._compute_rates(data)

        return data

    def reboot(self) -> bool:
        """Reboot the router (save config + reset board)."""
        cfg_html = self._fetch_page(URL_CFGFILE)
        if not cfg_html:
            _LOGGER.error("Reboot failed: could not load config page")
            return False
        m = RE_ONT_TOKEN.search(cfg_html)
        if not m:
            _LOGGER.error("Reboot failed: onttoken not found on config page")
            return False
        token = m.group(1)
        session = self._ensure_session()
        try:
            r = session.post(
                f"{self._base_url}{URL_REBOOT_CGI}",
                data={"x.X_HW_Token": token},
                timeout=15,
                allow_redirects=False,
            )
            _LOGGER.info("Reboot request sent: status %d", r.status_code)
            # router goes down after this; drop session state
            self._authenticated = False
            self._last_counters = None
            return r.status_code in (200, 302)
        except Exception as err:
            _LOGGER.error("Reboot request error: %s", err)
            self._authenticated = False
            return False

    def test_connection(self) -> bool:
        try:
            return self.authenticate()
        except Exception:
            return False

    def close(self) -> None:
        if self._session:
            self._session.close()
            self._session = None
        self._authenticated = False
