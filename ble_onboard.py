#!/usr/bin/env python3
"""BLE Onboarding Script — Automated BLE onboarding for Arlo DUT (Lory doorbell).

Connects to DUT via BLE, performs ECDH key exchange, passes WiFi credentials,
triggers WiFi connection, sends discovery token, and optionally claims via cloud.

Prerequisites:
  - serial_mux running (ISP on configured port)
  - BT adapter available (USB dongle or built-in)
  - pip install bleak cryptography playwright
  - playwright install chromium (for cloud claiming)
  - Copy ble_onboard.ini.template to ble_onboard.ini and fill in WiFi creds

Usage:
  python3 ble_onboard.py
  python3 ble_onboard.py --ssid "MyWiFi" --psk "password123"
  python3 ble_onboard.py --bt-adapter hci1 --skip-discovery
"""

import asyncio
import argparse
import configparser
import os
import struct
import sys
import time

from bleak import BleakScanner, BleakClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ble_onboard_constants import *
from ble_onboard_crypto import (
    generate_keypair,
    compute_shared_secret,
    encrypt_value,
    extract_pubkey_from_birth_cert,
)
from ble_onboard_serial import SerialMuxClient, disable_fota, extract_birth_cert
from ble_onboard_cloud import ArloCloudClient

sys.stdout.reconfigure(line_buffering=True)

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ble_onboard.ini")


def load_config(config_path):
    """Load configuration from .ini file."""
    config = configparser.ConfigParser()
    if os.path.exists(config_path):
        config.read(config_path)
    return config


def parse_args():
    parser = argparse.ArgumentParser(description="Automated BLE onboarding for Arlo DUT")
    parser.add_argument("--ssid", help="WiFi SSID (overrides config file)")
    parser.add_argument("--psk", help="WiFi passphrase (overrides config file)")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to .ini config file")
    parser.add_argument("--device-name", help="BLE advertising name to scan for")
    parser.add_argument("--device-mac", help="Skip scan, connect to specific BLE MAC")
    parser.add_argument("--device-pubkey", help="Path to device EC public key file (skip serial extraction)")
    parser.add_argument("--bt-adapter", help="Bluetooth adapter (e.g. hci0, hci1)")
    parser.add_argument("--band", type=int, help="WiFi band: 0=auto, 1=2.4GHz, 2=5GHz")
    parser.add_argument("--security", type=int, help="WiFi security: 0=auto, 2=WPA2, 3=WPA3")
    parser.add_argument("--serial-host", help="serial_mux host")
    parser.add_argument("--serial-port", type=int, help="serial_mux ISP port")
    parser.add_argument("--no-fota-disable", action="store_true", help="Skip FOTA disable step")
    parser.add_argument("--skip-discovery", action="store_true", help="Skip cloud claiming")
    parser.add_argument("--scan-wifi", action="store_true", help="Perform WiFi scan")
    parser.add_argument("--timeout", type=int, help="Total timeout in seconds")
    parser.add_argument("--verbose", action="store_true", help="Verbose BLE logging")
    return parser.parse_args()


class BleOnboarder:
    """Main BLE onboarding orchestrator."""

    def __init__(self, args, config):
        self.args = args
        self.config = config

        self.ssid = args.ssid or config.get("wifi", "ssid", fallback=None)
        self.psk = args.psk or config.get("wifi", "psk", fallback=None)
        self.band = args.band if args.band is not None else config.getint("wifi", "band", fallback=0)
        self.security = args.security if args.security is not None else config.getint("wifi", "security", fallback=0)

        self.device_name = args.device_name or config.get("device", "ble_name", fallback="AVD6001")
        self.device_mac = args.device_mac or config.get("device", "ble_mac", fallback=None)
        self.bt_adapter = args.bt_adapter or config.get("bluetooth", "adapter", fallback=None) or None

        self.serial_host = args.serial_host or config.get("serial", "host", fallback="localhost")
        self.serial_port = args.serial_port or config.getint("serial", "port", fallback=9001)
        self.serial_client_path = config.get("serial", "client_path", fallback=None)

        self.skip_fota = args.no_fota_disable or config.getboolean("options", "skip_fota_disable", fallback=False)
        self.skip_discovery = args.skip_discovery or config.getboolean("options", "skip_discovery", fallback=False)
        self.timeout = args.timeout or config.getint("options", "timeout", fallback=180)
        self.verbose = args.verbose or config.getboolean("options", "verbose", fallback=False)

        self.device_pubkey_bytes = None
        self.shared_secret_b64 = None
        self.private_key = None
        self.public_key_bytes = None
        self.discovery_token = None
        self.hardware_id = None
        self.client = None

        self._status_value = 0
        self._error_value = 0
        self._claiming_value = 0

    def validate(self):
        """Validate required parameters."""
        if not self.ssid:
            print("ERROR: WiFi SSID required (--ssid or config file)")
            return False
        if not self.psk:
            print("ERROR: WiFi passphrase required (--psk or config file)")
            return False
        if not self.serial_client_path:
            print("ERROR: serial_mux client_path not set in config [serial] section")
            return False
        if not os.path.exists(self.serial_client_path):
            print(f"ERROR: serial_mux_client not found at: {self.serial_client_path}")
            return False
        return True

    def phase0_serial_setup(self):
        """Phase 0: FOTA disable + birth cert extraction via serial_mux."""
        print("\n=== Phase 0: Serial Setup ===")
        serial = SerialMuxClient(self.serial_client_path, self.serial_host, self.serial_port)

        if not serial.check_connection():
            print("ERROR: Cannot reach serial_mux. Is it running?")
            return False

        if not self.skip_fota:
            if not disable_fota(serial):
                print("WARNING: FOTA disable may have failed, continuing anyway")

        if self.args.device_pubkey:
            print(f"  [CERT] Using provided pubkey file: {self.args.device_pubkey}")
            with open(self.args.device_pubkey, "rb") as f:
                self.device_pubkey_bytes = f.read()
            if len(self.device_pubkey_bytes) != COMMISSIONER_KEY_SIZE:
                print(f"ERROR: pubkey file must be {COMMISSIONER_KEY_SIZE} bytes (uncompressed), got {len(self.device_pubkey_bytes)}")
                return False
        else:
            cert = extract_birth_cert(serial)
            if not cert:
                print("ERROR: Failed to extract birth certificate")
                return False
            self.device_pubkey_bytes = extract_pubkey_from_birth_cert(cert)
            self.hardware_id = cert.get("certId", "")[:16]

        print("  Phase 0 complete")
        return True

    async def phase1_ble_connect(self):
        """Phase 1: BLE scan and connect."""
        print("\n=== Phase 1: BLE Connection ===")

        scanner_kwargs = {}
        if self.bt_adapter:
            scanner_kwargs["adapter"] = self.bt_adapter

        if self.device_mac:
            address = self.device_mac
            print(f"  Connecting to MAC: {address}")
        else:
            print(f"  Scanning for device '{self.device_name}'...")
            device = await self._scan_for_device(**scanner_kwargs)
            if not device:
                print("ERROR: Device not found. Is it in BLE advertising mode (SYNC button)?")
                return False
            address = device.address
            print(f"  Found: {device.name} ({device.address})")

        print(f"  Connecting...")
        client_kwargs = {}
        if self.bt_adapter:
            client_kwargs["adapter"] = self.bt_adapter
        self.client = BleakClient(address, **client_kwargs)
        await self.client.connect()

        if not self.client.is_connected:
            print("ERROR: BLE connection failed")
            return False

        print(f"  Connected (MTU={self.client.mtu_size})")
        return True

    async def _scan_for_device(self, **kwargs):
        """Scan for BLE device by name or manufacturer ID."""
        devices = await BleakScanner.discover(timeout=10.0, **kwargs)
        for d in devices:
            if d.name and self.device_name in d.name:
                return d
            if d.metadata and "manufacturer_data" in d.metadata:
                if ARLO_COMPANY_ID in d.metadata["manufacturer_data"]:
                    return d
        return None

    async def phase2_read_info(self):
        """Phase 2: Read device info characteristics."""
        print("\n=== Phase 2: Read Device Info ===")

        try:
            info_data = await self.client.read_gatt_char(CHAR_INFO)
            if info_data:
                version = info_data[0] if len(info_data) > 0 else 0
                print(f"  Protocol version: {version}")
                if self.verbose and len(info_data) > 2:
                    cert_id_bytes = info_data[2:18]
                    print(f"  Cert ID (from info): {cert_id_bytes.hex()}")
        except Exception as e:
            print(f"  Info read failed (non-fatal): {e}")

        try:
            fw_data = await self.client.read_gatt_char(CHAR_FW_VERSION)
            if fw_data:
                print(f"  FW version: {fw_data.decode('utf-8', errors='replace')}")
        except Exception as e:
            if self.verbose:
                print(f"  FW version read failed (v1 device?): {e}")

        return True

    async def phase3_key_exchange(self):
        """Phase 3: ECDH key exchange."""
        print("\n=== Phase 3: Key Exchange ===")

        self.private_key, self.public_key_bytes = generate_keypair()
        print(f"  Generated ephemeral EC P-256 keypair")
        if self.verbose:
            print(f"  Our pubkey: {self.public_key_bytes[:8].hex()}...({len(self.public_key_bytes)}B)")

        print(f"  Writing Commissioner Key ({len(self.public_key_bytes)}B)...")
        await self.client.write_gatt_char(CHAR_COMMISSIONER_KEY, self.public_key_bytes, response=True)

        self.shared_secret_b64 = compute_shared_secret(self.private_key, self.device_pubkey_bytes)
        print(f"  Shared secret computed")
        if self.verbose:
            print(f"  Shared secret (b64): {self.shared_secret_b64[:16]}...")

        return True

    async def phase4_set_mode(self):
        """Phase 4: Set onboarding mode."""
        print("\n=== Phase 4: Set Mode ===")

        mode_value = struct.pack(">H", ONBOARDING_MODE_D2AP_WIFI)
        print(f"  Writing mode: D2AP_WIFI (0x{ONBOARDING_MODE_D2AP_WIFI:04x})")
        await self.client.write_gatt_char(CHAR_MODE, mode_value, response=True)

        return True

    async def phase5_wifi_config(self):
        """Phase 5: Write WiFi configuration (encrypted)."""
        print("\n=== Phase 5: WiFi Configuration ===")

        wifi_mode = struct.pack("BB", self.band, self.security)
        print(f"  Writing WiFi mode: band={self.band}, security={self.security}")
        await self.client.write_gatt_char(CHAR_WIFI_MODE, wifi_mode, response=True)

        encrypted_ssid = encrypt_value(self.shared_secret_b64, self.ssid)
        print(f"  Writing encrypted SSID ({len(encrypted_ssid)}B)")
        await self.client.write_gatt_char(CHAR_WIFI_SSID, encrypted_ssid, response=True)

        encrypted_psk = encrypt_value(self.shared_secret_b64, self.psk)
        print(f"  Writing encrypted passphrase ({len(encrypted_psk)}B)")
        await self.client.write_gatt_char(CHAR_WIFI_PASSPHRASE, encrypted_psk, response=True)

        return True

    async def phase6_trigger_connect(self):
        """Phase 6: Trigger WiFi connection and wait for PAIRED status."""
        print("\n=== Phase 6: WiFi Connection ===")

        cmd_pair = struct.pack(">H", BLE_CMD_PAIR)
        print(f"  Writing command: PAIR")
        await self.client.write_gatt_char(CHAR_COMMAND, cmd_pair, response=True)

        print(f"  Waiting for WiFi connection (timeout 60s)...")
        start = time.time()
        while time.time() - start < 60:
            status_data = await self.client.read_gatt_char(CHAR_STATUS_ERROR)
            if len(status_data) >= 4:
                status = struct.unpack(">H", status_data[0:2])[0]
                error = struct.unpack(">H", status_data[2:4])[0]
                self._status_value = status
                self._error_value = error

                if self.verbose:
                    print(f"    status=0x{status:04x} ({status_to_str(status)}), error=0x{error:04x}")

                if status & STATUS_ERROR:
                    print(f"  ERROR: WiFi connection failed: {error_to_str(error)}")
                    return False

                if status & STATUS_PAIRED:
                    print(f"  WiFi connected + NTP synced (PAIRED)")
                    return True

                if status & STATUS_PAIRING:
                    if not self.verbose:
                        print(f"    ...connecting (status: {status_to_str(status)})")

            await asyncio.sleep(2)

        print(f"  ERROR: WiFi connection timeout (60s)")
        return False

    async def phase7_discovery(self):
        """Phase 7: Send discovery token."""
        print("\n=== Phase 7: Cloud Discovery ===")

        self.discovery_token = os.urandom(DISCOVERY_TOKEN_SIZE)
        discovery_hex = self.discovery_token.hex()
        print(f"  Discovery token: {discovery_hex}")

        encrypted_token = encrypt_value(self.shared_secret_b64, self.discovery_token)
        print(f"  Writing encrypted discovery token ({len(encrypted_token)}B)...")
        await self.client.write_gatt_char(CHAR_DISCOVERY_TOKEN, encrypted_token, response=True)

        print(f"  Waiting for cloud registration...")
        start = time.time()
        while time.time() - start < 70:
            claiming_data = await self.client.read_gatt_char(CHAR_CLAIMING_STATUS)
            if claiming_data:
                claiming = claiming_data[0]
                self._claiming_value = claiming

                if self.verbose:
                    print(f"    claiming=0x{claiming:02x} ({claiming_to_str(claiming)})")

                if claiming & CLAIMING_DISCOVERY_PUSHED:
                    print(f"  Discovery pushed to cloud")
                    print(f"  Claiming status: {claiming_to_str(claiming)}")
                    return True

                if claiming & CLAIMING_REGISTERED:
                    if not self.verbose:
                        print(f"    ...registered, waiting for discovery push")

            await asyncio.sleep(3)

        print(f"  ERROR: Discovery push timeout (70s)")
        return False

    def phase8_cloud_claim(self):
        """Phase 8: Claim device via Arlo cloud API."""
        print("\n=== Phase 8: Cloud Claim ===")

        email = self.config.get("arlo_cloud", "email", fallback=None)
        password = self.config.get("arlo_cloud", "password", fallback=None)
        auth_api = self.config.get("arlo_cloud", "auth_api", fallback=None)
        hmsweb_api = self.config.get("arlo_cloud", "hmsweb_api", fallback=None)
        site_url = self.config.get("arlo_cloud", "site_url", fallback=None)

        if not all([email, password, auth_api, hmsweb_api, site_url]):
            print("  ERROR: Cloud credentials incomplete in config file")
            print("  Set [arlo_cloud] section in ble_onboard.ini")
            return False

        cloud = ArloCloudClient(email, password, auth_api, hmsweb_api, site_url)
        try:
            cloud.connect()

            if self.hardware_id:
                device = cloud.wait_for_device(self.hardware_id, timeout=60)
                if device:
                    device_id = device.get("deviceId", "")
                    print(f"  Claiming device {device_id}...")
                    result = cloud.claim_device(device_id, self.hardware_id)
                    print(f"  Claim result: {result}")
                else:
                    print(f"  Device not found in cloud device list")
                    print(f"  (Device may auto-claim via discovery token)")
            else:
                print(f"  No hardware_id available, waiting for device to appear...")
                time.sleep(30)
                devices = cloud.get_devices()
                print(f"  Devices on account: {len(devices)}")
                for d in devices:
                    print(f"    - {d.get('deviceId', '?')}: {d.get('deviceName', '?')}")

        except Exception as e:
            print(f"  Cloud claim error: {e}")
            return False
        finally:
            cloud.close()

        return True

    async def phase9_wait_claimed(self):
        """Phase 9: Wait for DEVICE_CLAIMED on BLE."""
        print("\n=== Phase 9: Wait for Claimed ===")

        start = time.time()
        while time.time() - start < 90:
            try:
                claiming_data = await self.client.read_gatt_char(CHAR_CLAIMING_STATUS)
                if claiming_data:
                    claiming = claiming_data[0]
                    self._claiming_value = claiming

                    print(f"  Claiming: {claiming_to_str(claiming)}")

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
                if self.verbose:
                    print(f"    BLE read error: {e}")

            await asyncio.sleep(5)

        print(f"  Timeout waiting for DEVICE_CLAIMED (90s)")
        return False

    async def run(self):
        """Run the full onboarding sequence."""
        print("=" * 60)
        print("  BLE Onboarding Script")
        print("=" * 60)
        print(f"  SSID: {self.ssid}")
        print(f"  Band: {self.band}, Security: {self.security}")
        print(f"  Device: {self.device_name} (MAC: {self.device_mac or 'auto-scan'})")
        print(f"  BT adapter: {self.bt_adapter or 'default'}")
        print(f"  Skip FOTA: {self.skip_fota}, Skip discovery: {self.skip_discovery}")
        print()

        if not self.phase0_serial_setup():
            return 1

        input("\n  Press ENTER after putting DUT in BLE mode (SYNC button)...")

        if not await self.phase1_ble_connect():
            return 1

        try:
            await self.phase2_read_info()

            if not await self.phase3_key_exchange():
                return 1

            if not await self.phase4_set_mode():
                return 1

            if not await self.phase5_wifi_config():
                return 1

            if not await self.phase6_trigger_connect():
                return 1

            if self.skip_discovery:
                print("\n  === WiFi connection successful (discovery skipped) ===")
                return 0

            if not await self.phase7_discovery():
                return 1

            self.phase8_cloud_claim()

            if not await self.phase9_wait_claimed():
                print("\n  WARNING: DEVICE_CLAIMED not confirmed via BLE")
                print("  Device may still complete claiming asynchronously")
                return 1

        except Exception as e:
            print(f"\n  FATAL ERROR: {e}")
            if self.verbose:
                import traceback
                traceback.print_exc()
            return 1
        finally:
            if self.client and self.client.is_connected:
                print("\n  Disconnecting BLE...")
                await self.client.disconnect()

        print("\n  Onboarding complete!")
        return 0


def main():
    args = parse_args()
    config = load_config(args.config)

    onboarder = BleOnboarder(args, config)
    if not onboarder.validate():
        sys.exit(1)

    sys.exit(asyncio.run(onboarder.run()))


if __name__ == "__main__":
    main()
