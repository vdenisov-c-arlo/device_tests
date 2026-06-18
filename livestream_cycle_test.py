#!/usr/bin/env python3
"""Livestream Cycle Stress Test — reproduce MediaServer open_video_stream hang.

Uses Playwright to bypass Cloudflare and authenticate, then drives the full
livestream signaling flow (startStream → sipInfo → WebSocket SDP offer) to
trigger the device's video_ingress_worker → agw_mediasrvr_open_video_stream()
code path.

Monitors ISP/MCU consoles for the hang signature where video_ingress_worker
gets stuck in agw_mediasrvr_open_video_stream() (blocking msgrcv with no
timeout).

Prerequisites:
  - Device onboarded on goldendev, armed mode, battery power (forces sleep)
  - serial_mux running (ISP on configured ports, MCU on configured ports)
  - pip install playwright websockets && playwright install chromium

Usage:
  python3 livestream_cycle_test.py [--cycles 20] [--stream-duration 15]
"""

import socket
import time
import threading
import sys
import os
import json
import argparse
import uuid
import ssl
from datetime import datetime

try:
    import websockets.sync.client as ws_sync
except ImportError:
    print("ERROR: 'websockets' package required. Install: pip3 install websockets")
    sys.exit(1)

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from console_utils import get_serial_mux_config, isp_init_console
from mcu_patterns import is_crash_dump_line, save_crash_dump

sys.stdout.reconfigure(line_buffering=True)

_cfg = get_serial_mux_config()
MCU_HOST = _cfg['mcu_host']
MCU_PORT = _cfg['mcu_port']
ISP_HOST = _cfg['isp_host']
ISP_PORT = _cfg['isp_port']
LOG_DIR = "/tmp/livestream_cycle_logs"

SLEEP_INDICATOR = "Network Stack Suspended, MCU can enter DeepSleep power mode"
MEDIASERVER_OPEN = "MediaServer Open Params"
STREAM_ACTIVE = "userStreamActive"
MEDIASERVER_OK = "mediasrvr_open_video_stream ok"
MEDIASERVER_FAIL = "unable to open video stream in MediaServer"
SPROP_OK = "sprop-parameter-set="
INGRESS_WORKER = "_media_ingress_worker()"
THREAD_REGISTER = "register_thread() video_ingress_worker"
THREAD_UNREGISTER = "unregister_thread() video_ingress_worker"

CRASH_PATTERNS = [
    "segfault", "kernel panic", "coredump", "Core dump",
    "Assertion failed", "Oops:", "erpc error: 14",
    "HardFault", "BusFault", "MemManage",
]

GOLDENDEV_EMAIL = "voodoojah@gmail.com"
GOLDENDEV_PASSWORD = "UmVwelJlcHozNDcy"
GOLDENDEV_SITE = "https://mygoldendev.arlo.com"
AUTH_API = "https://myapigdev-web.arlo.com"
HMSWEB_API = "https://myapigoldendev.arlo.com"
LIVESTREAM_WSS = "wss://livestream-z2-goldendev.arlo.com:7443/"

SDP_OFFER_TEMPLATE = (
    "v=0\r\n"
    "o=- {session_id} 2 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0 1\r\n"
    "a=extmap-allow-mixed\r\n"
    "a=msid-semantic: WMS\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8 13 110 126\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=rtcp:9 IN IP4 0.0.0.0\r\n"
    "a=ice-ufrag:{ice_ufrag}\r\n"
    "a=ice-pwd:{ice_pwd}\r\n"
    "a=ice-options:trickle\r\n"
    "a=fingerprint:sha-256 {fingerprint}\r\n"
    "a=setup:actpass\r\n"
    "a=mid:0\r\n"
    "a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level\r\n"
    "a=extmap:2 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time\r\n"
    "a=extmap:3 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01\r\n"
    "a=extmap:4 urn:ietf:params:rtp-hdrext:sdes:mid\r\n"
    "a=sendrecv\r\n"
    "a=msid:- {audio_msid}\r\n"
    "a=rtcp-mux\r\n"
    "a=rtcp-rsize\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=rtcp-fb:111 transport-cc\r\n"
    "a=fmtp:111 minptime=10;useinbandfec=1\r\n"
    "a=rtpmap:63 red/48000/2\r\n"
    "a=fmtp:63 111/111\r\n"
    "a=rtpmap:9 G722/8000\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=rtpmap:13 CN/8000\r\n"
    "a=rtpmap:110 telephone-event/48000\r\n"
    "a=rtpmap:126 telephone-event/8000\r\n"
    "a=ssrc:{audio_ssrc} cname:{cname}\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 96 97 98 99 100 101 127\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=rtcp:9 IN IP4 0.0.0.0\r\n"
    "a=ice-ufrag:{ice_ufrag}\r\n"
    "a=ice-pwd:{ice_pwd}\r\n"
    "a=ice-options:trickle\r\n"
    "a=fingerprint:sha-256 {fingerprint}\r\n"
    "a=setup:actpass\r\n"
    "a=mid:1\r\n"
    "a=extmap:2 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time\r\n"
    "a=extmap:3 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01\r\n"
    "a=extmap:4 urn:ietf:params:rtp-hdrext:sdes:mid\r\n"
    "a=recvonly\r\n"
    "a=rtcp-mux\r\n"
    "a=rtcp-rsize\r\n"
    "a=rtpmap:96 H264/90000\r\n"
    "a=rtcp-fb:96 goog-remb\r\n"
    "a=rtcp-fb:96 transport-cc\r\n"
    "a=rtcp-fb:96 ccm fir\r\n"
    "a=rtcp-fb:96 nack\r\n"
    "a=rtcp-fb:96 nack pli\r\n"
    "a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f\r\n"
    "a=rtpmap:97 rtx/90000\r\n"
    "a=fmtp:97 apt=96\r\n"
    "a=rtpmap:98 H264/90000\r\n"
    "a=rtcp-fb:98 goog-remb\r\n"
    "a=rtcp-fb:98 transport-cc\r\n"
    "a=rtcp-fb:98 ccm fir\r\n"
    "a=rtcp-fb:98 nack\r\n"
    "a=rtcp-fb:98 nack pli\r\n"
    "a=fmtp:98 level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=42001f\r\n"
    "a=rtpmap:99 rtx/90000\r\n"
    "a=fmtp:99 apt=98\r\n"
    "a=rtpmap:100 H264/90000\r\n"
    "a=rtcp-fb:100 goog-remb\r\n"
    "a=rtcp-fb:100 transport-cc\r\n"
    "a=rtcp-fb:100 ccm fir\r\n"
    "a=rtcp-fb:100 nack\r\n"
    "a=rtcp-fb:100 nack pli\r\n"
    "a=fmtp:100 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f\r\n"
    "a=rtpmap:101 rtx/90000\r\n"
    "a=fmtp:101 apt=100\r\n"
    "a=rtpmap:127 H264/90000\r\n"
    "a=rtcp-fb:127 goog-remb\r\n"
    "a=rtcp-fb:127 transport-cc\r\n"
    "a=rtcp-fb:127 ccm fir\r\n"
    "a=rtcp-fb:127 nack\r\n"
    "a=rtcp-fb:127 nack pli\r\n"
    "a=fmtp:127 level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=42e01f\r\n"
)


def generate_sdp_offer():
    """Generate a unique but valid SDP offer for each stream session."""
    import hashlib
    import random

    session_id = str(random.randint(1000000000000000000, 9999999999999999999))
    ice_ufrag = uuid.uuid4().hex[:4]
    ice_pwd = uuid.uuid4().hex[:24]
    fp_bytes = hashlib.sha256(uuid.uuid4().bytes).hexdigest().upper()
    fingerprint = ":".join(fp_bytes[i:i+2] for i in range(0, 64, 2))
    audio_msid = str(uuid.uuid4())
    audio_ssrc = random.randint(1000000000, 4294967295)
    cname = uuid.uuid4().hex[:16]

    return SDP_OFFER_TEMPLATE.format(
        session_id=session_id,
        ice_ufrag=ice_ufrag,
        ice_pwd=ice_pwd,
        fingerprint=fingerprint,
        audio_msid=audio_msid,
        audio_ssrc=audio_ssrc,
        cname=cname,
    )


class ConsoleReader(threading.Thread):
    """Continuously reads from serial_mux TCP socket, stores lines."""

    def __init__(self, name, host, port, log_file, init_func=None):
        super().__init__(daemon=True)
        self.name_tag = name
        self.host = host
        self.port = port
        self.log_file = log_file
        self.init_func = init_func
        self.lines = []
        self.lock = threading.Lock()
        self.events = []
        self.connected = False

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((self.host, self.port))
            sock.settimeout(1.0)
            self.connected = True
            self._sock = sock
            self._initialized = False
            self._init_pending = False
            buf = b""
            with open(self.log_file, "a") as f:
                while True:
                    try:
                        data = sock.recv(4096)
                        if not data:
                            break
                        buf += data
                        if self.init_func and b"login:" in data:
                            time.sleep(1)
                            self.init_func(sock)
                            self._initialized = True
                            self._init_pending = False
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            text = line.decode("utf-8", errors="replace").strip()
                            if text:
                                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                                entry = f"[{ts}] [{self.name_tag}] {text}"
                                f.write(entry + "\n")
                                f.flush()
                                with self.lock:
                                    self.lines.append(text)
                                    self._check_events(text)
                    except socket.timeout:
                        if self._init_pending and self.init_func and not self._initialized:
                            self._init_pending = False
                            time.sleep(1)
                            self.init_func(sock)
                            self._initialized = True
                        continue
        except Exception as e:
            print(f"  [{self.name_tag}] Connection failed: {e}")

    def reinit(self):
        """Request re-initialization from the reader thread."""
        self._initialized = False
        self._init_pending = True

    def _check_events(self, text):
        if SLEEP_INDICATOR in text:
            self.events.append(("sleep", time.time(), text))
        elif MEDIASERVER_OPEN in text or STREAM_ACTIVE in text:
            self.events.append(("ms_open", time.time(), text))
        elif MEDIASERVER_OK in text or SPROP_OK in text or INGRESS_WORKER in text:
            self.events.append(("ms_ok", time.time(), text))
        elif MEDIASERVER_FAIL in text:
            self.events.append(("ms_fail", time.time(), text))
        elif "video_ingress_worker" in text and "unregister_thread" in text:
            self.events.append(("ingress_exit", time.time(), text))
        elif is_crash_dump_line(text):
            self.events.append(("crash", time.time(), text))
        elif any(p in text for p in CRASH_PATTERNS):
            self.events.append(("crash", time.time(), text))

    def get_events_since(self, since_time):
        with self.lock:
            return [(ev, t, txt) for ev, t, txt in self.events if t > since_time]

    def clear_events(self):
        with self.lock:
            self.events.clear()


class ArloStreamAPI:
    """Arlo cloud API + WebRTC signaling via Playwright fetch + raw WebSocket."""

    def __init__(self, device_id, model_id="AVD6001"):
        self.device_id = device_id
        self.model_id = model_id
        self.pw = None
        self.browser = None
        self.page = None
        self.token = None
        self.parent_id = None
        self.xcloud_id = None
        self.unique_id = None
        self._ws = None
        self._sip_info = None

    def connect(self):
        print("  Launching Playwright for API auth...")
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = self.browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        )
        self.page = context.new_page()
        self.page.add_init_script(
            'Object.defineProperty(navigator, "webdriver", {get: () => undefined});'
        )
        print("  Navigating to goldendev (CF challenge ~15s)...")
        self.page.goto(GOLDENDEV_SITE, timeout=90000)
        time.sleep(15)
        self._authenticate()

    def _authenticate(self):
        result = self.page.evaluate("""async ([authApi, hmsApi, email, password, deviceId]) => {
            function txId() {
                return 'FE!' + ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
                    (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
            }

            const authResp = await fetch(authApi + '/ocapi/accounts/v1/auth', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                credentials: 'include',
                body: JSON.stringify({email, password, language: 'en', EnvSource: 'goldendev'})
            });
            const authData = await authResp.json();
            if (!authData.data || !authData.data.token) return {error: 'auth failed', data: authData};
            const token = authData.data.token;

            const sid = txId();
            const sessResp = await fetch(hmsApi + '/hmsweb/users/session/v3?eventId=' + sid + '&time=' + Date.now(), {
                headers: {'auth-version': '2', 'Authorization': token, 'content-type': 'application/json; charset=utf-8',
                          'origin': 'https://mygoldendev.arlo.com', 'x-transaction-id': sid},
                credentials: 'include'
            });
            if (sessResp.status !== 200) return {error: 'session failed', status: sessResp.status};

            const did = txId();
            const devResp = await fetch(hmsApi + '/hmsweb/v2/users/devices?t=' + Date.now() + '&eventId=' + did + '&time=' + Date.now(), {
                headers: {'auth-version': '2', 'Authorization': token, 'content-type': 'application/json; charset=utf-8',
                          'origin': 'https://mygoldendev.arlo.com', 'x-transaction-id': did},
                credentials: 'include'
            });
            const devData = await devResp.json();
            if (!devData.data) return {error: 'devices failed', status: devResp.status};

            let device = null;
            for (const d of devData.data) {
                if (d.deviceId === deviceId) { device = d; break; }
            }
            if (!device) return {error: 'device not found', ids: devData.data.map(d => d.deviceId)};

            return {
                token,
                parentId: device.parentId || deviceId,
                xCloudId: device.xCloudId || '',
                uniqueId: device.uniqueId || '',
                modelId: device.modelId || '',
                deviceName: device.deviceName
            };
        }""", [AUTH_API, HMSWEB_API, GOLDENDEV_EMAIL, GOLDENDEV_PASSWORD, self.device_id])

        if result.get("error"):
            raise RuntimeError(f"Auth failed: {result}")

        self.token = result["token"]
        self.parent_id = result["parentId"]
        self.xcloud_id = result["xCloudId"]
        self.unique_id = result.get("uniqueId", "")
        if result.get("modelId"):
            self.model_id = result["modelId"]
        print(f"  API connected: device={result['deviceName']}, model={self.model_id}, parent={self.parent_id}")

    def start_stream(self):
        """Full livestream start: notify device + SDP signaling.

        Returns True if the signaling completed (device should open MediaServer).
        """
        # Step 1: Send startUserStream notify to wake device
        print(f"    [1/4] Sending startUserStream notify...")
        notify_ok = self._send_start_notify()
        if not notify_ok:
            return False

        # Step 2: Wait for device to boot (ISP cold boot takes ~7s)
        print(f"    [2/4] Waiting 10s for device to boot...")
        time.sleep(10)

        # Step 3: Get SIP info for signaling
        print(f"    [3/4] Fetching sipInfo...")
        sip_info = self._get_sip_info()
        if not sip_info:
            return False
        self._sip_info = sip_info

        # Step 4: Open WebSocket and send SDP offer
        print(f"    [4/4] Sending SDP offer via WebSocket...")
        return self._send_sdp_offer(sip_info)

    def _send_start_notify(self):
        result = self.page.evaluate("""async ([hmsApi, token, parentId, xCloudId, deviceId]) => {
            function txId() {
                return 'FE!' + ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
                    (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
            }
            const sid = txId();
            const body = {
                to: parentId,
                from: parentId + '_web',
                resource: 'cameras/' + deviceId,
                action: 'set',
                responseUrl: '',
                publishResponse: true,
                transId: sid,
                properties: {activityState: 'startUserStream', cameraId: deviceId}
            };
            const resp = await fetch(hmsApi + '/hmsweb/users/devices/startStream', {
                method: 'POST',
                headers: {'auth-version': '2', 'Authorization': token, 'content-type': 'application/json; charset=UTF-8',
                          'origin': 'https://mygoldendev.arlo.com', 'xcloudid': xCloudId, 'x-transaction-id': sid},
                credentials: 'include',
                body: JSON.stringify(body)
            });
            const text = await resp.text();
            return {status: resp.status, body: text.substring(0, 500)};
        }""", [HMSWEB_API, self.token, self.parent_id, self.xcloud_id, self.device_id])

        if result.get("status") != 200:
            print(f"      startStream failed: status={result.get('status')}")
            print(f"      body: {result.get('body', '')[:200]}")
            return False
        return True

    def _get_sip_info(self):
        result = self.page.evaluate("""async ([hmsApi, token, xCloudId, deviceId, modelId, uniqueId]) => {
            function txId() {
                return 'FE!' + ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
                    (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
            }
            const sid = txId();
            const url = hmsApi + '/hmsweb/users/devices/sipInfo/v2?cameraId=' + deviceId +
                        '&modelId=' + modelId + '&uniqueId=' + uniqueId +
                        '&eventId=' + sid + '&time=' + Date.now();
            const resp = await fetch(url, {
                headers: {'auth-version': '2', 'Authorization': token,
                          'origin': 'https://mygoldendev.arlo.com', 'xcloudid': xCloudId,
                          'cameraid': deviceId,
                          'content-type': 'application/json; charset=utf-8',
                          'x-transaction-id': sid},
                credentials: 'include'
            });
            if (resp.status !== 200) return {error: 'sipInfo failed', status: resp.status};
            const data = await resp.json();
            return data;
        }""", [HMSWEB_API, self.token, self.xcloud_id, self.device_id, self.model_id, self.unique_id])

        if result.get("error"):
            print(f"      sipInfo failed: {result}")
            return None
        if not result.get("data", {}).get("sipCallInfo"):
            print(f"      sipInfo: no sipCallInfo in response")
            print(f"      response keys: {list(result.get('data', {}).keys())}")
            return None

        sip = result["data"]["sipCallInfo"]
        print(f"      sipCallInfo: domain={sip['domain']}, callId={sip.get('callId', '?')}")
        return result["data"]

    def _send_sdp_offer(self, sip_data):
        sip_info = sip_data["sipCallInfo"]
        domain = sip_info["domain"]
        port = 7443  # Always 7443 for WebSocket (sipInfo port 443 is for SIP URI)
        wss_url = f"wss://{domain}:{port}/"

        sdp_offer = generate_sdp_offer()
        session_id = str(uuid.uuid4())

        payload = {
            "sipCallInfo": {
                "calleeUri": sip_info["calleeUri"],
                "id": sip_info["id"],
                "password": sip_info["password"],
                "domain": f"{domain}:{port}",
                "port": str(port),
            },
            "payload": {
                "sessionId": session_id,
                "cameraId": self.device_id,
                "offer": {
                    "format": "SDP",
                    "value": sdp_offer,
                },
            },
        }

        body_json = json.dumps(payload)
        content_length = len(body_json.encode())

        http_msg = (
            f"POST /hmswebsocketproxy/initiateOffer HTTP/1.1\r\n"
            f"Host: {domain}:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Connection: keep-alive\r\n"
            f"Accept: */*\r\n"
            f"User-Agent: ArloWebRTC/1 CFNetwork/1329 Darwin/21.3.0\r\n"
            f"Content-Length: {content_length}\r\n"
            f"Accept-Language: en-IN,en-GB;q=0.9,en;q=0.8\r\n"
            f"Accept-Encoding: gzip, deflate, br\r\n"
            f"\r\n"
            f"{body_json}"
        )

        try:
            ssl_ctx = ssl.create_default_context()
            self._ws = ws_sync.connect(
                wss_url,
                subprotocols=["sip"],
                additional_headers={"Origin": "https://mygoldendev.arlo.com"},
                ssl_context=ssl_ctx,
                open_timeout=15,
            )
            self._ws.send(http_msg)

            # Wait for SDP answer (200 OK)
            response = self._ws.recv(timeout=20)
            if "200 OK" in response:
                print(f"      Got 200 OK with SDP answer ({len(response)} bytes)")
                return True
            else:
                print(f"      Unexpected WS response: {response[:200]}")
                return False
        except Exception as e:
            print(f"      WebSocket signaling failed: {e}")
            return False

    def stop_stream(self):
        """Send sessionDisconnected to end the stream."""
        if not self._ws or not self._sip_info:
            return False

        sip_info = self._sip_info["sipCallInfo"]
        domain = sip_info["domain"]
        port = 7443

        payload = {
            "sipCallInfo": {
                "calleeUri": sip_info["calleeUri"],
                "id": sip_info["id"],
                "password": sip_info["password"],
                "domain": f"{domain}:{port}",
                "port": str(port),
            },
            "payload": {
                "sessionId": str(uuid.uuid4()),
                "cameraId": self.device_id,
            },
        }

        body_json = json.dumps(payload)
        content_length = len(body_json.encode())

        http_msg = (
            f"POST /hmswebsocketproxy/sessionDisconnected HTTP/1.1\r\n"
            f"Host: {domain}:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Connection: keep-alive\r\n"
            f"Accept: */*\r\n"
            f"User-Agent: ArloWebRTC/1 CFNetwork/1329 Darwin/21.3.0\r\n"
            f"Content-Length: {content_length}\r\n"
            f"Accept-Language: en-IN,en-GB;q=0.9,en;q=0.8\r\n"
            f"Accept-Encoding: gzip, deflate, br\r\n"
            f"\r\n"
            f"{body_json}"
        )

        try:
            self._ws.send(http_msg)
            time.sleep(1)
            self._ws.close()
        except Exception:
            pass
        self._ws = None
        self._sip_info = None

        # Also send idle notify via API
        self.page.evaluate("""async ([hmsApi, token, parentId, xCloudId, deviceId]) => {
            function txId() {
                return 'FE!' + ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
                    (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
            }
            const nid = txId();
            const body = {
                from: parentId + '_web',
                to: parentId,
                action: 'set',
                resource: 'cameras/' + deviceId,
                publishResponse: true,
                transId: nid,
                properties: {activityState: 'idle', cameraId: deviceId}
            };
            await fetch(hmsApi + '/hmsweb/users/devices/notify/' + parentId, {
                method: 'POST',
                headers: {'auth-version': '2', 'Authorization': token, 'content-type': 'application/json; charset=UTF-8',
                          'origin': 'https://mygoldendev.arlo.com', 'xcloudid': xCloudId, 'x-transaction-id': nid},
                credentials: 'include',
                body: JSON.stringify(body)
            });
        }""", [HMSWEB_API, self.token, self.parent_id, self.xcloud_id, self.device_id])
        return True

    def close(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self.browser:
            self.browser.close()
        if self.pw:
            self.pw.stop()


def wait_for_event(reader, event_type, timeout, since):
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = reader.get_events_since(since)
        for ev, t, txt in events:
            if ev == event_type:
                return (ev, t, txt)
        time.sleep(0.5)
    return None


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VOODOO_SCRIPT = os.path.join(SCRIPT_DIR, "voodoo_do_pulse.py")


def voodoo(voodoo_args):
    import subprocess
    cmd = [sys.executable, VOODOO_SCRIPT] + voodoo_args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return result.returncode == 0


def reset_device_livestream(isp_reader, mcu_reader):
    """Hardware-reset the DUT via voodoo board reset button and wait for boot."""
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] [RESET] Pressing reset button...")
    voodoo(["2", "1"])
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] [RESET] Waiting 60s for device to boot...")
    isp_reader.clear_events()
    mcu_reader.clear_events()
    time.sleep(60)


def handle_crash_livestream(isp_reader, mcu_reader, cycle, output_dir):
    """Save crash dump from livestream test, reset device, continue."""
    time.sleep(3)
    lines = []
    with isp_reader.lock:
        lines += list(getattr(isp_reader, '_raw_lines', []))[-30:]
    with mcu_reader.lock:
        lines += list(getattr(mcu_reader, '_raw_lines', []))[-30:]

    # Collect from events
    crash_events = [(t, txt) for ev, t, txt in isp_reader.events + mcu_reader.events
                    if ev == "crash"]
    for _, txt in crash_events:
        if txt not in lines:
            lines.append(txt)

    dump_path = save_crash_dump(lines, output_dir, "livestream", cycle, source="isp")
    if dump_path:
        print(f"  [DUMP] Crash dump saved: {os.path.basename(dump_path)}")
    else:
        os.makedirs(output_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback = os.path.join(output_dir, f"crash_livestream_cycle{cycle}_{ts}.log")
        with open(fallback, "w") as f:
            f.write(f"# Crash context — livestream cycle {cycle}\n")
            f.write(f"# Time: {datetime.now().isoformat()}\n\n")
            for l in lines[-30:]:
                f.write(l + "\n")
        dump_path = fallback
        print(f"  [DUMP] Context saved: {os.path.basename(fallback)}")

    reset_device_livestream(isp_reader, mcu_reader)
    return dump_path


def run_test(args):
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(LOG_DIR, f"stream_cycle_{timestamp}.log")

    print(f"=== Livestream Cycle Stress Test ===")
    print(f"  Device: {args.device_id}")
    print(f"  Cycles: {args.cycles}")
    print(f"  Stream duration: {args.stream_duration}s")
    print(f"  Sleep timeout: {args.sleep_timeout}s")
    print(f"  Log: {log_file}")
    print()

    # Start console readers
    isp_reader = ConsoleReader("ISP", ISP_HOST, ISP_PORT, log_file, init_func=isp_init_console)
    mcu_reader = ConsoleReader("MCU", MCU_HOST, MCU_PORT, log_file)
    isp_reader.start()
    mcu_reader.start()
    time.sleep(1)
    if not isp_reader.connected:
        print("  WARNING: ISP console not connected (serial_mux?)")
    if not mcu_reader.connected:
        print("  WARNING: MCU console not connected (serial_mux?)")

    # Connect API
    api = ArloStreamAPI(args.device_id)
    try:
        api.connect()
    except Exception as e:
        print(f"  FATAL: API connection failed: {e}")
        sys.exit(1)

    results = []
    hung_detected = False

    for cycle in range(1, args.cycles + 1):
        print(f"\n--- Cycle {cycle}/{args.cycles} ---")
        cycle_start = time.time()
        isp_reader.clear_events()
        mcu_reader.clear_events()

        # Step 1: Start livestream (full signaling)
        print(f"  [{datetime.now().strftime('%H:%M:%S')}] Starting livestream...")
        if not api.start_stream():
            print(f"  WARN: Stream start failed at signaling level")
            results.append(("signaling_fail", cycle))
            time.sleep(10)
            continue

        # Step 2: Wait for MediaServer Open
        print(f"  Waiting for MediaServer Open (timeout 30s)...")
        ms_event = wait_for_event(isp_reader, "ms_open", 30, cycle_start)
        if ms_event is None:
            print(f"  WARN: No MediaServer Open within 30s")
            results.append(("no_ms_open", cycle))
            api.stop_stream()
            time.sleep(10)
            continue

        open_time = ms_event[1]
        print(f"  [{datetime.now().strftime('%H:%M:%S')}] MediaServer Open logged")

        # Step 3: Check outcome (success / fail / hang)
        outcome = None
        deadline = time.time() + 20
        while time.time() < deadline:
            events = isp_reader.get_events_since(open_time - 0.1)
            for ev, t, txt in events:
                if ev == "ms_ok":
                    outcome = "ok"
                    break
                elif ev == "ms_fail":
                    outcome = "fail"
                    break
                elif ev == "crash":
                    outcome = "crash"
                    break
            if outcome:
                break
            time.sleep(0.5)

        if outcome == "ok":
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] Stream opened OK")
            print(f"  Streaming for {args.stream_duration}s...")
            time.sleep(args.stream_duration)
        elif outcome == "fail":
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] *** MediaServer OPEN FAILED (recoverable)")
            results.append(("open_failed", cycle))
        elif outcome == "crash":
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] !!! CRASH DETECTED (unrelated)")
            results.append(("crash", cycle))
            api.stop_stream()
            dump_dir = getattr(args, 'output_dir', None) or os.path.join(SCRIPT_DIR, "crash_dumps")
            handle_crash_livestream(isp_reader, mcu_reader, cycle, dump_dir)
            continue
        else:
            # No response in 20s — THIS IS THE HANG
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] *** HANG DETECTED — MediaServer never responded!")
            print(f"  video_ingress_worker stuck in msgrcv()")
            results.append(("HUNG", cycle))
            hung_detected = True

            print(f"  Confirming hang (60s observation)...")
            time.sleep(60)
            exit_events = isp_reader.get_events_since(open_time)
            ingress_exited = any(ev == "ingress_exit" for ev, _, _ in exit_events)
            if not ingress_exited:
                print(f"  CONFIRMED: video_ingress_worker still stuck after 60s")
                print(f"  Device will NOT sleep until reboot.")
            else:
                print(f"  False alarm: video_ingress_worker eventually exited (slow open)")
                results[-1] = ("slow_open", cycle)
                hung_detected = False

            if hung_detected:
                print(f"\n  === BUG REPRODUCED at cycle {cycle} ===")
                print(f"  Log saved: {log_file}")
                break

        # Step 4: Stop livestream
        print(f"  [{datetime.now().strftime('%H:%M:%S')}] Stopping livestream...")
        api.stop_stream()

        # Step 5: Wait for sleep
        print(f"  Waiting for sleep (timeout {args.sleep_timeout}s)...")
        stop_time = time.time()
        sleep_event = wait_for_event(mcu_reader, "sleep", args.sleep_timeout, stop_time)
        if sleep_event:
            sleep_latency = sleep_event[1] - stop_time
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] Device sleeping (took {sleep_latency:.1f}s)")
            results.append(("ok", cycle))
            time.sleep(3)
        else:
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] *** Device did NOT sleep within {args.sleep_timeout}s!")
            events_after_stop = isp_reader.get_events_since(stop_time)
            has_ingress_exit = any(ev == "ingress_exit" for ev, _, _ in events_after_stop)
            if not has_ingress_exit and ms_event:
                print(f"  Likely hung: video_ingress_worker never unregistered")
                results.append(("HUNG_NO_SLEEP", cycle))
                hung_detected = True
                print(f"\n  === BUG REPRODUCED (sleep failure) at cycle {cycle} ===")
                print(f"  Log saved: {log_file}")
                break
            else:
                results.append(("no_sleep_other", cycle))
                time.sleep(10)

    # Summary
    print(f"\n{'='*60}")
    print(f"=== TEST COMPLETE ===")
    print(f"  Total cycles: {len(results)}")
    ok_count = sum(1 for r, _ in results if r == 'ok')
    hung_count = sum(1 for r, _ in results if r in ('HUNG', 'HUNG_NO_SLEEP'))
    print(f"  Successful streams: {ok_count}")
    print(f"  Open failed (recoverable): {sum(1 for r, _ in results if r == 'open_failed')}")
    print(f"  HUNG (msgrcv blocked): {hung_count}")
    print(f"  Crashes: {sum(1 for r, _ in results if r == 'crash')}")
    print(f"  No MediaServer Open: {sum(1 for r, _ in results if r == 'no_ms_open')}")
    print(f"  Signaling failures: {sum(1 for r, _ in results if r == 'signaling_fail')}")
    print(f"  No sleep (other): {sum(1 for r, _ in results if r == 'no_sleep_other')}")
    print(f"  Log file: {log_file}")
    if hung_detected:
        print(f"\n  *** BUG REPRODUCED — MediaServer hang confirmed ***")
    print(f"{'='*60}")

    api.close()
    return 1 if hung_detected else 0


def main():
    parser = argparse.ArgumentParser(description="Livestream cycle test for MediaServer hang")
    parser.add_argument("--device-id", default="ALJ15BKX000EA",
                        help="Device ID (default: ALJ15BKX000EA)")
    parser.add_argument("--cycles", type=int, default=20,
                        help="Number of stream cycles (default: 20)")
    parser.add_argument("--stream-duration", type=int, default=15,
                        help="Seconds to keep stream active per cycle (default: 15)")
    parser.add_argument("--sleep-timeout", type=int, default=90,
                        help="Max seconds to wait for device sleep (default: 90)")
    parser.add_argument("--output-dir", "-o", default=None,
                        help="Directory for crash dump files (default: ./crash_dumps)")
    args = parser.parse_args()

    sys.exit(run_test(args))


if __name__ == "__main__":
    main()
