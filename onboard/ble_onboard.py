#!/usr/bin/env python3
"""BLE Onboarding Script — Automated BLE onboarding for Arlo DUT (Lory doorbell).

Each step can be run individually or all steps run sequentially.
State is persisted to a JSON file between steps so you can debug step-by-step.

Steps:
  wake       Wake device from shipping mode (15s long press + 5x short presses)
  fota       Disable FOTA update URL via serial console
  scan       BLE scan, connect, and read device info (cert_id, fw version)
  pubkey     Fetch device EC public key from Arlo cloud using cert_id
  exchange   ECDH key exchange — write commissioner key, compute shared secret
  mode       Set onboarding mode to D2AP_WIFI
  wifi       Write encrypted WiFi credentials (SSID + passphrase)
  connect    Send PAIR command and wait for WiFi connection (PAIRED status)
  discovery  Send encrypted discovery token, wait for cloud registration
  claim      Claim device via Arlo cloud API
  wait       Wait for DEVICE_CLAIMED status on BLE
  all        Run all steps sequentially (default)

Prerequisites:
  pip install bleak cryptography playwright
  playwright install chromium

Usage:
  python3 ble_onboard.py all                  # Full onboarding from scratch
  python3 ble_onboard.py wake                 # Just wake device
  python3 ble_onboard.py scan                 # Just scan and connect
  python3 ble_onboard.py scan exchange wifi connect  # Multiple steps
  python3 ble_onboard.py --no-wait scan       # Skip wake, device already in BLE mode
  python3 ble_onboard.py --verbose all        # Full run with debug output
  python3 ble_onboard.py --help               # Show all options
"""

import asyncio
import argparse
import base64
import configparser
import json
import os
import struct
import sys
import time

from bleak import BleakScanner, BleakClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from onboard.ble_onboard_constants import *
from onboard.ble_onboard_crypto import (
    generate_keypair,
    compute_shared_secret,
    encrypt_value,
)
from onboard.ble_onboard_serial import SerialMuxClient, disable_fota
from onboard.ble_onboard_cloud import ArloCloudClient
from testbot4.testbot4_do_pulse import connect as testbot4_connect, write_do, read_do

SYNC_BUTTON_DO = 0
sys.stdout.reconfigure(line_buffering=True)

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ble_onboard.ini")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ble_onboard_state.json")

ALL_STEPS = ["wake", "fota", "scan", "pubkey", "exchange", "mode", "wifi", "connect", "discovery", "claim", "wait"]


def load_config(config_path):
    config = configparser.ConfigParser()
    if os.path.exists(config_path):
        config.read(config_path)
    return config


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"  [state] Saved to {STATE_FILE}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="BLE onboarding for Arlo DUT — run steps individually or all at once",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Steps (run in order, or pick individual ones):
  wake       Wake from shipping mode (long + short SYNC presses via testbot4)
  fota       Disable FOTA URL via serial_mux (prevents firmware update during test)
  scan       BLE scan by name, connect, read cert_id and FW version
  pubkey     Fetch device public key from Arlo cloud (needs cert_id from scan)
  exchange   Write our EC P-256 public key to device, compute ECDH shared secret
  mode       Set onboarding mode to D2AP_WIFI
  wifi       Encrypt and write WiFi SSID + passphrase
  connect    Send PAIR command, poll until WiFi connected (PAIRED)
  discovery  Send encrypted discovery token, wait for cloud push
  claim      Locate device in cloud and claim it
  wait       Poll BLE for DEVICE_CLAIMED confirmation
  all        Run all steps in sequence

State is saved to .ble_onboard_state.json between steps.
Use --clear-state to start fresh.

Examples:
  %(prog)s all                     Full automated onboarding
  %(prog)s --no-wake all           Skip wake (device already in BLE mode)
  %(prog)s wake                    Just trigger wake sequence
  %(prog)s scan                    Just scan and connect BLE
  %(prog)s pubkey exchange         Fetch pubkey then do key exchange
  %(prog)s wifi connect            Write WiFi creds and trigger connection
  %(prog)s --clear-state scan      Clear saved state, then scan
""",
    )
    parser.add_argument("steps", nargs="*", default=["all"],
                        help="Steps to run (default: all). See list below.")
    parser.add_argument("--ssid", help="WiFi SSID (overrides config)")
    parser.add_argument("--psk", help="WiFi passphrase (overrides config)")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to .ini config")
    parser.add_argument("--device-name", help="BLE advertising name to scan for")
    parser.add_argument("--bt-adapter", help="Bluetooth adapter (hci0, hci1, ...)")
    parser.add_argument("--band", type=int, help="WiFi band: 0=auto, 1=2.4GHz, 2=5GHz")
    parser.add_argument("--security", type=int, help="WiFi security: 0=auto, 2=WPA2, 3=WPA3")
    parser.add_argument("--serial-host", help="serial_mux host")
    parser.add_argument("--serial-port", type=int, help="serial_mux ISP port")
    parser.add_argument("--no-wake", action="store_true", help="Skip wake sequence (device already up)")
    parser.add_argument("--verbose", action="store_true", help="Verbose BLE logging")
    parser.add_argument("--clear-state", action="store_true", help="Clear saved state before running")
    parser.add_argument("--scan-timeout", type=int, default=30, help="BLE scan timeout seconds (default 30)")
    return parser.parse_args()


class BleOnboarder:
    """BLE onboarding orchestrator with per-step execution."""

    def __init__(self, args, config):
        self.args = args
        self.config = config
        self.verbose = args.verbose

        self.ssid = args.ssid or config.get("wifi", "ssid", fallback=None)
        self.psk = args.psk or config.get("wifi", "psk", fallback=None)
        self.band = args.band if args.band is not None else config.getint("wifi", "band", fallback=0)
        self.security = args.security if args.security is not None else config.getint("wifi", "security", fallback=0)

        self.device_name = args.device_name or config.get("device", "ble_name", fallback="AVD6001")
        self.bt_adapter = args.bt_adapter or config.get("bluetooth", "adapter", fallback=None) or None

        self.serial_host = args.serial_host or config.get("serial", "host", fallback="localhost")
        self.serial_port = args.serial_port or config.getint("serial", "port", fallback=9001)
        self.serial_client_path = config.get("serial", "client_path", fallback=None)

        self.scan_timeout = args.scan_timeout

        self.state = load_state() if not args.clear_state else {}
        self.client = None
        self.cloud = None

    # ------------------------------------------------------------------
    # BLE helpers
    # ------------------------------------------------------------------

    async def ble_scan_and_connect(self):
        """Scan for device by name and establish BLE connection.

        Always does a fresh scan — never uses a cached address.
        Returns True if connected.
        """
        scanner_kwargs = {}
        if self.bt_adapter:
            scanner_kwargs["adapter"] = self.bt_adapter

        print(f"  Scanning for '{self.device_name}' ({self.scan_timeout}s)...")

        device = None
        for attempt in range(3):
            try:
                device = await BleakScanner.find_device_by_name(
                    self.device_name, timeout=self.scan_timeout, **scanner_kwargs
                )
                if device:
                    print(f"  Found: {device.name} ({device.address})")
                    break

                # Fallback: discover all and match by name or manufacturer data
                print(f"  find_device_by_name missed, trying discover()...")
                devices = await BleakScanner.discover(timeout=10.0, **scanner_kwargs)
                for d in devices:
                    ad = getattr(d, "advertising_data", None)
                    local_name = ad.local_name if ad and hasattr(ad, "local_name") else None
                    if self.verbose:
                        print(f"    {d.address} | name={d.name!r} local={local_name!r}")
                    if (d.name and self.device_name in d.name) or \
                       (local_name and self.device_name in local_name):
                        device = d
                        print(f"  Found (fallback): {d.name} ({d.address})")
                        break
                    if ad and hasattr(ad, "manufacturer_data") and ARLO_COMPANY_ID in ad.manufacturer_data:
                        device = d
                        print(f"  Found (Arlo mfr ID): {d.address}")
                        break

                if device:
                    break

            except Exception as e:
                print(f"  Scan error: {e}")

            if attempt < 2:
                print(f"  Attempt {attempt+1}/3 failed, retrying in 3s...")
                await asyncio.sleep(3)

        if not device:
            print("  ERROR: Device not found. Is it in BLE advertising mode?")
            return False

        # Connect
        client_kwargs = {}
        if self.bt_adapter:
            client_kwargs["adapter"] = self.bt_adapter

        for attempt in range(3):
            try:
                if attempt > 0:
                    print(f"  Retry {attempt+1}/3 in 5s...")
                    await asyncio.sleep(5)
                self.client = BleakClient(device, **client_kwargs)
                await self.client.connect(timeout=30.0)
                print(f"  Connected (MTU={self.client.mtu_size})")
                return True
            except Exception as e:
                print(f"  Connect error: {e}")
                self.client = None

        print("  ERROR: BLE connection failed after 3 attempts")
        return False

    async def ensure_ble(self):
        """Ensure we have an active BLE connection. Scan fresh if needed."""
        if self.client and self.client.is_connected:
            return True
        print("  [ble] Connecting...")
        return await self.ble_scan_and_connect()

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def step_wake(self):
        """Wake device from shipping mode via testbot4 SYNC button."""
        print("\n=== STEP: wake ===")
        from testbot4.testbot4_do_pulse import MODBUS_TCP_PORT, DEFAULT_HOST
        sock = testbot4_connect(DEFAULT_HOST, MODBUS_TCP_PORT)
        try:
            print("  Long SYNC press (15s) — wake from shipping...")
            cur = read_do(sock, 1)
            on_val = cur | (1 << SYNC_BUTTON_DO)
            write_do(sock, on_val, 2)
            time.sleep(15)
            off_val = on_val & ~(1 << SYNC_BUTTON_DO)
            write_do(sock, off_val, 3)
            print("  Released")

            print("  Waiting for MCU console (60s max)...")
            mcu_port = 9002
            serial = SerialMuxClient(self.serial_client_path, self.serial_host, mcu_port)
            if serial.wait_for_output(timeout_s=60, poll_interval=2):
                print("  MCU active")
            else:
                print("  WARNING: No MCU output after 60s, continuing")

            print("  5x short SYNC presses (3s each)...")
            time.sleep(2)
            for i in range(5):
                cur = read_do(sock, 1)
                on_val = cur | (1 << SYNC_BUTTON_DO)
                write_do(sock, on_val, 10 + i * 2)
                time.sleep(3)
                off_val = on_val & ~(1 << SYNC_BUTTON_DO)
                write_do(sock, off_val, 11 + i * 2)
                print(f"    Press {i+1}/5 done")
                if i < 4:
                    time.sleep(2)
        finally:
            sock.close()

        print("  Waiting 10s for BLE advertising...")
        time.sleep(10)
        print("  DONE")
        return True

    def step_fota(self):
        """Disable FOTA update URL via serial console."""
        print("\n=== STEP: fota ===")
        serial = SerialMuxClient(self.serial_client_path, self.serial_host, self.serial_port)
        if not serial.check_connection():
            print("  ERROR: serial_mux not reachable")
            return False
        result = disable_fota(serial)
        print(f"  DONE (success={result})")
        return True

    async def step_scan(self):
        """Scan for device, connect via BLE, read cert_id and FW version."""
        print("\n=== STEP: scan ===")

        if not await self.ble_scan_and_connect():
            return False

        # Read info characteristic
        cert_id_hex = None
        try:
            info_data = await self.client.read_gatt_char(CHAR_INFO)
            if info_data:
                version = info_data[0]
                print(f"  Protocol version: {version}")
                if len(info_data) > 2 and version <= 3:
                    cert_id_hex = info_data[2:18].hex()
                    print(f"  Cert ID (from info v1): {cert_id_hex}")
        except Exception as e:
            print(f"  Info read error: {e}")

        # V2+ protocol: cert_id is separate characteristic
        if not cert_id_hex:
            try:
                cert_id_data = await self.client.read_gatt_char(CHAR_CERTIFICATE_ID)
                if cert_id_data:
                    cert_id_hex = cert_id_data.hex()
                    print(f"  Cert ID (from characteristic): {cert_id_hex}")
            except Exception as e:
                print(f"  Certificate ID read error: {e}")

        # FW version
        try:
            fw_data = await self.client.read_gatt_char(CHAR_FW_VERSION)
            if fw_data:
                fw_str = fw_data.decode("utf-8", errors="replace")
                print(f"  FW version: {fw_str}")
                self.state["fw_version"] = fw_str
        except Exception as e:
            if self.verbose:
                print(f"  FW version read error: {e}")

        if not cert_id_hex:
            print("  ERROR: Could not read certificate ID")
            return False

        self.state["cert_id"] = cert_id_hex
        save_state(self.state)
        print(f"  DONE — cert_id={cert_id_hex}")
        return True

    def step_pubkey(self):
        """Fetch device EC P-256 public key from Arlo cloud."""
        print("\n=== STEP: pubkey ===")

        cert_id = self.state.get("cert_id")
        if not cert_id:
            print("  ERROR: No cert_id in state. Run 'scan' step first.")
            return False

        email = self.config.get("arlo_cloud", "email", fallback=None)
        password = self.config.get("arlo_cloud", "password", fallback=None)
        auth_api = self.config.get("arlo_cloud", "auth_api", fallback=None)
        hmsweb_api = self.config.get("arlo_cloud", "hmsweb_api", fallback=None)
        site_url = self.config.get("arlo_cloud", "site_url", fallback=None)

        if not all([email, password, auth_api, hmsweb_api, site_url]):
            print("  ERROR: Cloud credentials required in [arlo_cloud] config section")
            return False

        model_id = self.device_name
        device_id = self.state.get("device_id", "")

        cloud = ArloCloudClient(email, password, auth_api, hmsweb_api, site_url)
        try:
            cloud.connect()
            self.cloud = cloud

            print(f"  Fetching cert for certId={cert_id[:16]}... model={model_id}")
            cert_data = cloud.get_device_cert(cert_id, model_id, device_id)
            if not cert_data:
                print("  ERROR: Cloud returned no data")
                return False

            print(f"  Response: {json.dumps(cert_data, indent=2)[:600]}")

            data = cert_data.get("data", cert_data)
            pub_key_b64 = data.get("publicKey") or data.get("public_key")
            if not pub_key_b64:
                print(f"  ERROR: No publicKey in response. Keys: {list(data.keys())}")
                return False

            pub_key_bytes = base64.b64decode(pub_key_b64)
            if len(pub_key_bytes) == 64:
                pub_key_bytes = b'\x04' + pub_key_bytes
                print(f"  Prepended 0x04 prefix (was raw 64-byte x||y)")

            print(f"  Device public key: {len(pub_key_bytes)} bytes")
            if self.verbose:
                print(f"  Hex: {pub_key_bytes.hex()}")

            self.state["device_pubkey"] = base64.b64encode(pub_key_bytes).decode()
            save_state(self.state)
            print(f"  DONE")
            return True

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def step_exchange(self):
        """ECDH key exchange — write our pubkey, compute shared secret."""
        print("\n=== STEP: exchange ===")

        pub_key_b64 = self.state.get("device_pubkey")
        if not pub_key_b64:
            print("  ERROR: No device_pubkey in state. Run 'pubkey' step first.")
            return False

        device_pubkey_bytes = base64.b64decode(pub_key_b64)
        if len(device_pubkey_bytes) != COMMISSIONER_KEY_SIZE:
            print(f"  ERROR: device pubkey is {len(device_pubkey_bytes)}B, need {COMMISSIONER_KEY_SIZE}")
            return False

        if not await self.ensure_ble():
            return False

        private_key, our_pubkey_bytes = generate_keypair()
        print(f"  Generated ephemeral EC P-256 keypair ({len(our_pubkey_bytes)}B)")
        if self.verbose:
            print(f"  Our pubkey: {our_pubkey_bytes.hex()[:32]}...")

        print(f"  Writing Commissioner Key...")
        await self.client.write_gatt_char(CHAR_COMMISSIONER_KEY, our_pubkey_bytes, response=True)
        print(f"  Written OK")

        shared_secret_b64 = compute_shared_secret(private_key, device_pubkey_bytes)
        print(f"  Shared secret computed")
        if self.verbose:
            print(f"  Shared secret (b64): {shared_secret_b64[:20]}...")

        self.state["shared_secret"] = shared_secret_b64
        self.state["our_pubkey"] = base64.b64encode(our_pubkey_bytes).decode()
        save_state(self.state)
        print(f"  DONE")
        return True

    async def step_mode(self):
        """Set onboarding mode to D2AP_WIFI."""
        print("\n=== STEP: mode ===")

        if not await self.ensure_ble():
            return False

        mode_value = struct.pack(">H", ONBOARDING_MODE_D2AP_WIFI)
        print(f"  Writing mode: D2AP_WIFI (0x{ONBOARDING_MODE_D2AP_WIFI:04x})")
        await self.client.write_gatt_char(CHAR_MODE, mode_value, response=True)
        print(f"  DONE")
        return True

    async def step_wifi(self):
        """Encrypt and write WiFi SSID + passphrase."""
        print("\n=== STEP: wifi ===")

        if not self.ssid or not self.psk:
            print("  ERROR: WiFi SSID and PSK required (--ssid/--psk or config)")
            return False

        shared_secret = self.state.get("shared_secret")
        if not shared_secret:
            print("  ERROR: No shared_secret in state. Run 'exchange' step first.")
            return False

        if not await self.ensure_ble():
            return False

        wifi_mode = struct.pack("BB", self.band, self.security)
        print(f"  WiFi mode: band={self.band}, security={self.security}")
        await self.client.write_gatt_char(CHAR_WIFI_MODE, wifi_mode, response=True)

        encrypted_ssid = encrypt_value(shared_secret, self.ssid)
        print(f"  Writing encrypted SSID '{self.ssid}' ({len(encrypted_ssid)}B)")
        await self.client.write_gatt_char(CHAR_WIFI_SSID, encrypted_ssid, response=True)

        encrypted_psk = encrypt_value(shared_secret, self.psk)
        print(f"  Writing encrypted passphrase ({len(encrypted_psk)}B)")
        await self.client.write_gatt_char(CHAR_WIFI_PASSPHRASE, encrypted_psk, response=True)

        print(f"  DONE")
        return True

    async def step_connect(self):
        """Send PAIR command and wait for WiFi connection."""
        print("\n=== STEP: connect ===")

        if not await self.ensure_ble():
            return False

        cmd_pair = struct.pack(">H", BLE_CMD_PAIR)
        print(f"  Sending PAIR command...")
        await self.client.write_gatt_char(CHAR_COMMAND, cmd_pair, response=True)

        print(f"  Waiting for WiFi connection (60s timeout)...")
        start = time.time()
        while time.time() - start < 60:
            status_data = await self.client.read_gatt_char(CHAR_STATUS_ERROR)
            if len(status_data) >= 4:
                status = struct.unpack(">H", status_data[0:2])[0]
                error = struct.unpack(">H", status_data[2:4])[0]

                status_str = status_to_str(status)
                if self.verbose or status != 0:
                    print(f"    status=0x{status:04x} ({status_str}), error=0x{error:04x}")

                if status & STATUS_ERROR:
                    print(f"  ERROR: WiFi failed — {error_to_str(error)}")
                    return False

                if status & STATUS_PAIRED:
                    print(f"  WiFi connected + NTP synced (PAIRED)")
                    print(f"  DONE")
                    return True

            await asyncio.sleep(2)

        print(f"  ERROR: Timeout waiting for WiFi connection")
        return False

    async def step_discovery(self):
        """Send encrypted discovery token, wait for cloud push."""
        print("\n=== STEP: discovery ===")

        shared_secret = self.state.get("shared_secret")
        if not shared_secret:
            print("  ERROR: No shared_secret in state. Run 'exchange' step first.")
            return False

        if not await self.ensure_ble():
            return False

        discovery_token = os.urandom(DISCOVERY_TOKEN_SIZE)
        discovery_hex = discovery_token.hex()
        print(f"  Discovery token: {discovery_hex}")

        encrypted_token = encrypt_value(shared_secret, discovery_token)
        print(f"  Writing encrypted discovery token ({len(encrypted_token)}B)...")
        await self.client.write_gatt_char(CHAR_DISCOVERY_TOKEN, encrypted_token, response=True)

        self.state["discovery_token"] = discovery_hex
        save_state(self.state)

        print(f"  Waiting for cloud registration (70s)...")
        start = time.time()
        while time.time() - start < 70:
            claiming_data = await self.client.read_gatt_char(CHAR_CLAIMING_STATUS)
            if claiming_data:
                claiming = claiming_data[0]
                cs = claiming_to_str(claiming)
                if self.verbose or claiming != 0:
                    print(f"    claiming=0x{claiming:02x} ({cs})")

                if claiming & CLAIMING_DISCOVERY_PUSHED:
                    print(f"  Discovery pushed to cloud")
                    print(f"  DONE")
                    return True

            await asyncio.sleep(3)

        print(f"  ERROR: Discovery push timeout")
        return False

    def step_claim(self):
        """Locate and claim device via Arlo cloud API."""
        print("\n=== STEP: claim ===")

        discovery_hex = self.state.get("discovery_token")
        if not discovery_hex:
            print("  ERROR: No discovery_token in state. Run 'discovery' step first.")
            return False

        email = self.config.get("arlo_cloud", "email", fallback=None)
        password = self.config.get("arlo_cloud", "password", fallback=None)
        auth_api = self.config.get("arlo_cloud", "auth_api", fallback=None)
        hmsweb_api = self.config.get("arlo_cloud", "hmsweb_api", fallback=None)
        site_url = self.config.get("arlo_cloud", "site_url", fallback=None)

        if not all([email, password, auth_api, hmsweb_api, site_url]):
            print("  ERROR: Cloud credentials required in [arlo_cloud] config section")
            return False

        if not self.cloud:
            cloud = ArloCloudClient(email, password, auth_api, hmsweb_api, site_url)
            cloud.connect()
            self.cloud = cloud

        try:
            print(f"  Locating device with discovery token {discovery_hex}...")
            locate_result = self.cloud.locate_device(discovery_hex)
            if locate_result and not locate_result.get("error"):
                print(f"  Locate result: {json.dumps(locate_result, indent=2)[:500]}")
                data = locate_result.get("data", locate_result)
                xcloud_id = data.get("xCloudId") or data.get("xcloudId", "")
                device_id = data.get("deviceId", "")
                if xcloud_id and device_id:
                    print(f"  Claiming: device={device_id}, xcloud={xcloud_id}")
                    model = self.device_name + "A"
                    claim_result = self.cloud.claim_device_v2(device_id, xcloud_id, discovery_hex, model)
                    print(f"  Claim result: {claim_result}")
                    self.state["device_id"] = device_id
                    self.state["xcloud_id"] = xcloud_id
                    save_state(self.state)
                    print(f"  DONE")
                    return True
                else:
                    print(f"  No xCloudId/deviceId in locate response")
            else:
                print(f"  Locate failed: {locate_result}")

            # Fallback: wait for device to appear in device list
            print(f"  Trying device list polling (60s)...")
            start = time.time()
            while time.time() - start < 60:
                devices = self.cloud.get_devices()
                if devices:
                    print(f"  Found {len(devices)} device(s) on account")
                    for d in devices:
                        print(f"    {d.get('deviceId', '?')}: {d.get('deviceName', '?')}")
                    print(f"  DONE (device may auto-claim via discovery)")
                    return True
                time.sleep(5)

            print(f"  ERROR: Could not claim device")
            return False

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def step_wait(self):
        """Wait for DEVICE_CLAIMED status on BLE."""
        print("\n=== STEP: wait ===")

        if not await self.ensure_ble():
            return False

        print(f"  Polling BLE for CLAIMED status (90s)...")
        start = time.time()
        while time.time() - start < 90:
            try:
                claiming_data = await self.client.read_gatt_char(CHAR_CLAIMING_STATUS)
                if claiming_data:
                    claiming = claiming_data[0]
                    print(f"    claiming=0x{claiming:02x} ({claiming_to_str(claiming)})")

                    if claiming & CLAIMING_DEVICE_CLAIMED:
                        print(f"\n  === DEVICE CLAIMED SUCCESSFULLY ===")
                        return True

                status_data = await self.client.read_gatt_char(CHAR_STATUS_ERROR)
                if len(status_data) >= 4:
                    status = struct.unpack(">H", status_data[0:2])[0]
                    if status & STATUS_CLAIMED_D2AP:
                        print(f"\n  === ONBOARDING COMPLETE (CLAIMED_D2AP) ===")
                        return True
            except Exception as e:
                print(f"    BLE read error: {e}")

            await asyncio.sleep(5)

        print(f"  Timeout waiting for DEVICE_CLAIMED (90s)")
        return False

    # ------------------------------------------------------------------
    # Runner
    # ------------------------------------------------------------------

    async def run_steps(self, steps):
        """Execute the requested steps in order."""
        print("=" * 60)
        print("  BLE Onboarding")
        print("=" * 60)
        print(f"  Steps: {' → '.join(steps)}")
        print(f"  Device: {self.device_name}")
        print(f"  WiFi: ssid={self.ssid}, band={self.band}, sec={self.security}")
        if self.state:
            print(f"  State: {list(self.state.keys())}")
        print()

        try:
            for step_name in steps:
                ok = await self._run_one(step_name)
                if not ok:
                    print(f"\n  FAILED at step '{step_name}'")
                    return 1
        finally:
            if self.client and self.client.is_connected:
                print("\n  Disconnecting BLE...")
                await self.client.disconnect()
            if self.cloud:
                self.cloud.close()

        print(f"\n  All steps completed successfully!")
        return 0

    async def _run_one(self, step_name):
        """Dispatch a single step by name."""
        if step_name == "wake":
            return self.step_wake()
        elif step_name == "fota":
            return self.step_fota()
        elif step_name == "scan":
            return await self.step_scan()
        elif step_name == "pubkey":
            return self.step_pubkey()
        elif step_name == "exchange":
            return await self.step_exchange()
        elif step_name == "mode":
            return await self.step_mode()
        elif step_name == "wifi":
            return await self.step_wifi()
        elif step_name == "connect":
            return await self.step_connect()
        elif step_name == "discovery":
            return await self.step_discovery()
        elif step_name == "claim":
            return self.step_claim()
        elif step_name == "wait":
            return await self.step_wait()
        else:
            print(f"  ERROR: Unknown step '{step_name}'")
            print(f"  Valid steps: {', '.join(ALL_STEPS)}")
            return False


def main():
    args = parse_args()
    config = load_config(args.config)

    # Expand "all" into the full step list
    steps = []
    for s in args.steps:
        if s == "all":
            if args.no_wake:
                steps.extend(ALL_STEPS[1:])  # skip wake
            else:
                steps.extend(ALL_STEPS)
        else:
            steps.append(s)

    # Validate step names
    for s in steps:
        if s not in ALL_STEPS:
            print(f"ERROR: Unknown step '{s}'")
            print(f"Valid steps: {', '.join(ALL_STEPS + ['all'])}")
            sys.exit(1)

    onboarder = BleOnboarder(args, config)

    # Validate serial_mux path for steps that need it
    if any(s in steps for s in ["fota", "wake"]):
        if not onboarder.serial_client_path:
            print("ERROR: serial_mux client_path not set in config [serial] section")
            sys.exit(1)
        if not os.path.exists(onboarder.serial_client_path):
            print(f"ERROR: serial_mux_client not found at: {onboarder.serial_client_path}")
            sys.exit(1)

    # Validate WiFi params for steps that need them
    if "wifi" in steps:
        if not onboarder.ssid or not onboarder.psk:
            print("ERROR: WiFi SSID and PSK required (--ssid/--psk or config file)")
            sys.exit(1)

    sys.exit(asyncio.run(onboarder.run_steps(steps)))


if __name__ == "__main__":
    main()
