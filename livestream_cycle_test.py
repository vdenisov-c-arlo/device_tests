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

import time
import sys
import os
import json
import argparse
import uuid
import ssl
from datetime import datetime
from enum import Enum, auto

try:
    import websockets.sync.client as ws_sync
except ImportError:
    print("ERROR: 'websockets' package required. Install: pip3 install websockets")
    sys.exit(1)

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from console_utils import DeviceTestBase, isp_init_console
from mcu_patterns import is_crash_dump_line, save_crash_dump

sys.stdout.reconfigure(line_buffering=True)

LOG_DIR = "/tmp/livestream_cycle_logs"

SLEEP_INDICATOR = "Network Stack Suspended, MCU can enter DeepSleep power mode"
MEDIASERVER_OPEN = "MediaServer Open Params"
STREAM_ACTIVE = "userStreamActive"
MEDIASERVER_OK = "mediasrvr_open_video_stream ok"
MEDIASERVER_FAIL = "unable to open video stream in MediaServer"
SPROP_OK = "sprop-parameter-set="
INGRESS_WORKER = "_media_ingress_worker()"
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

RESET_DO_CHANNEL = 2

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


class Event(Enum):
    SLEEP_DETECTED = auto()
    MS_OPEN = auto()
    MS_OK = auto()
    MS_FAIL = auto()
    INGRESS_EXIT = auto()
    CRASH_DETECTED = auto()


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
        print(f"    [1/4] Sending startUserStream notify...")
        notify_ok = self._send_start_notify()
        if not notify_ok:
            return False

        print(f"    [2/4] Waiting 10s for device to boot...")
        time.sleep(10)

        print(f"    [3/4] Fetching sipInfo...")
        sip_info = self._get_sip_info()
        if not sip_info:
            return False
        self._sip_info = sip_info

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
        port = 7443
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


class LivestreamCycleTest(DeviceTestBase):
    _test_name = "livestream_cycle"
    _log_dir = LOG_DIR
    _sleep_timeout = 90

    def __init__(self, device_id, stream_duration, sleep_timeout, output_dir=None):
        super().__init__()
        self.device_id = device_id
        self.stream_duration = stream_duration
        self._sleep_timeout = sleep_timeout
        self.output_dir = output_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "crash_dumps")
        self.api = None
        self.hung_detected = False

    def _check_events(self, line, source):
        if source == "MCU":
            if SLEEP_INDICATOR in line:
                self.event_callback(Event.SLEEP_DETECTED, source, line)

        if MEDIASERVER_OPEN in line or STREAM_ACTIVE in line:
            self.event_callback(Event.MS_OPEN, source, line)
        elif MEDIASERVER_OK in line or SPROP_OK in line or INGRESS_WORKER in line:
            self.event_callback(Event.MS_OK, source, line)
        elif MEDIASERVER_FAIL in line:
            self.event_callback(Event.MS_FAIL, source, line)
        elif "video_ingress_worker" in line and "unregister_thread" in line:
            self.event_callback(Event.INGRESS_EXIT, source, line)
        elif is_crash_dump_line(line):
            self.event_callback(Event.CRASH_DETECTED, source, line)
        elif any(p in line for p in CRASH_PATTERNS):
            self.event_callback(Event.CRASH_DETECTED, source, line)

    def _handle_crash(self, cycle):
        """Save crash dump, reset device."""
        time.sleep(3)
        lines = []
        if self.isp:
            lines += self.isp.get_lines()
        if self.mcu:
            lines += self.mcu.get_lines()

        dump_path = save_crash_dump(lines, self.output_dir, "livestream", cycle, source="isp")
        if dump_path:
            print(f"  [DUMP] Crash dump saved: {os.path.basename(dump_path)}")
        else:
            os.makedirs(self.output_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fallback = os.path.join(self.output_dir, f"crash_livestream_cycle{cycle}_{ts}.log")
            with open(fallback, "w") as f:
                f.write(f"# Crash context — livestream cycle {cycle}\n")
                f.write(f"# Time: {datetime.now().isoformat()}\n\n")
                for l in lines[-30:]:
                    f.write(l + "\n")
            print(f"  [DUMP] Context saved: {os.path.basename(fallback)}")

        print(f"  [RESET] Pressing reset button...")
        self.press_button(RESET_DO_CHANNEL, 1.0)
        print(f"  [RESET] Waiting 60s for device to boot...")
        self.clear_events()
        time.sleep(60)

    def run_cycle(self, cycle_num):
        print(f"\n--- Cycle {cycle_num} ---")
        self.clear_events()
        self.mcu.start_recording()
        self.isp.start_recording()

        # Start livestream
        print(f"  [{datetime.now().strftime('%H:%M:%S')}] Starting livestream...")
        if not self.api.start_stream():
            print(f"  WARN: Stream start failed at signaling level")
            self.mcu.stop_recording()
            self.isp.stop_recording()
            time.sleep(10)
            return True  # signaling failure is not a device bug

        # Wait for MediaServer Open
        print(f"  Waiting for MediaServer Open (timeout 30s)...")
        ms_event = self.wait_for_event(Event.MS_OPEN, timeout=30)
        if ms_event is None:
            print(f"  WARN: No MediaServer Open within 30s")
            self.api.stop_stream()
            self.mcu.stop_recording()
            self.isp.stop_recording()
            time.sleep(10)
            return True  # no open is not a hang

        print(f"  [{datetime.now().strftime('%H:%M:%S')}] MediaServer Open logged")

        # Check outcome
        outcome_event = self.wait_for_any_event(
            [Event.MS_OK, Event.MS_FAIL, Event.CRASH_DETECTED], timeout=20)

        if outcome_event and outcome_event[0] == Event.MS_OK:
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] Stream opened OK")
            print(f"  Streaming for {self.stream_duration}s...")
            time.sleep(self.stream_duration)
        elif outcome_event and outcome_event[0] == Event.MS_FAIL:
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] *** MediaServer OPEN FAILED (recoverable)")
        elif outcome_event and outcome_event[0] == Event.CRASH_DETECTED:
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] !!! CRASH DETECTED")
            self.api.stop_stream()
            self._handle_crash(cycle_num)
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return True  # crash is unrelated, device reset
        else:
            # No response in 20s — THIS IS THE HANG
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] *** HANG DETECTED — MediaServer never responded!")
            print(f"  video_ingress_worker stuck in msgrcv()")

            print(f"  Confirming hang (60s observation)...")
            time.sleep(60)
            exit_event = self.check_event(Event.INGRESS_EXIT)
            if not exit_event:
                print(f"  CONFIRMED: video_ingress_worker still stuck after 60s")
                print(f"  Device will NOT sleep until reboot.")
                self.hung_detected = True
                self.api.stop_stream()
                self.mcu.stop_recording()
                self.isp.stop_recording()
                return False
            else:
                print(f"  False alarm: video_ingress_worker eventually exited (slow open)")

        # Stop livestream
        print(f"  [{datetime.now().strftime('%H:%M:%S')}] Stopping livestream...")
        self.api.stop_stream()

        # Wait for sleep
        print(f"  Waiting for sleep (timeout {self._sleep_timeout}s)...")
        sleep_event = self.wait_for_event(Event.SLEEP_DETECTED, timeout=self._sleep_timeout)
        self.mcu.stop_recording()
        self.isp.stop_recording()

        if sleep_event:
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] Device sleeping")
            time.sleep(3)
            return True
        else:
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] *** Device did NOT sleep within {self._sleep_timeout}s!")
            exit_event = self.check_event(Event.INGRESS_EXIT)
            if not exit_event:
                print(f"  Likely hung: video_ingress_worker never unregistered")
                self.hung_detected = True
                return False
            time.sleep(10)
            return True  # no-sleep for other reason

    def recovery(self, cycle):
        print(f"\n  [RECOVERY] Resetting device...")
        self.press_button(RESET_DO_CHANNEL, 1.0)
        time.sleep(3)
        self.reconnect_consoles()
        self.clear_events()
        print(f"  [RECOVERY] Waiting 60s for boot...")
        time.sleep(60)
        if self.isp and self.isp.sock:
            isp_init_console(self.isp.sock)
        return True

    def run(self, num_cycles=1):
        os.makedirs(self._log_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)

        print(f"=== Livestream Cycle Stress Test ===")
        print(f"  Device: {self.device_id}")
        print(f"  Cycles: {num_cycles}")
        print(f"  Stream duration: {self.stream_duration}s")
        print(f"  Sleep timeout: {self._sleep_timeout}s")
        print()

        # Connect consoles
        print("[INIT] Connecting consoles...")
        self.connect_consoles()
        time.sleep(1)

        # Init ISP console
        if self.isp and self.isp.sock:
            isp_init_console(self.isp.sock)

        # Connect API
        self.api = ArloStreamAPI(self.device_id)
        try:
            self.api.connect()
        except Exception as e:
            print(f"  FATAL: API connection failed: {e}")
            self.disconnect_consoles()
            return 1

        # Run cycles
        for cycle in range(1, num_cycles + 1):
            passed = self.run_cycle(cycle)
            self.results.append(passed)

            if not passed:
                if self.hung_detected:
                    print(f"\n  === BUG REPRODUCED at cycle {cycle} ===")
                    break
                if not self.recovery(cycle):
                    print("  [RECOVERY] Aborting remaining cycles")
                    break

        self.api.close()
        self.disconnect_consoles()

        # Summary
        total = len(self.results)
        ok_count = sum(self.results)
        print(f"\n{'='*60}")
        print(f"=== TEST COMPLETE ===")
        print(f"  Total cycles: {total}")
        print(f"  Successful: {ok_count}")
        print(f"  Failed: {total - ok_count}")
        if self.hung_detected:
            print(f"\n  *** BUG REPRODUCED — MediaServer hang confirmed ***")
        print(f"{'='*60}")

        return 1 if self.hung_detected else 0


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

    test = LivestreamCycleTest(
        device_id=args.device_id,
        stream_duration=args.stream_duration,
        sleep_timeout=args.sleep_timeout,
        output_dir=args.output_dir,
    )
    sys.exit(test.run(num_cycles=args.cycles))


if __name__ == "__main__":
    main()
