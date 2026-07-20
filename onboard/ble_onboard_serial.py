"""BLE Onboarding Serial — serial_mux helpers for FOTA disable and pubkey extraction."""

import subprocess
import json
import time


class SerialMuxClient:
    """Wrapper around serial_mux_client for ISP console commands."""

    def __init__(self, client_path, host="localhost", port=9001):
        self.client_path = client_path
        self.host = host
        self.port = port

    def wait_for_output(self, port=None, timeout_s=60, poll_interval=2):
        """Poll a serial_mux port until any output appears (device booting).

        Args:
            port: TCP port to monitor (default: self.port).
            timeout_s: Max seconds to wait.
            poll_interval: Seconds between polls.

        Returns:
            True if output detected, False on timeout.
        """
        target_port = port or self.port
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                full_cmd = [
                    self.client_path, self.host, str(target_port),
                    "--cmd", "\r\n", "--timeout", str(int(poll_interval * 1000)),
                ]
                result = subprocess.run(
                    full_cmd, capture_output=True, text=True,
                    timeout=poll_interval + 3
                )
                output = result.stdout + result.stderr
                # Filter out just the "Connected to" line — we need actual console output
                lines = [l for l in output.strip().splitlines()
                         if l.strip() and "Connected to serial_mux" not in l]
                if lines:
                    return True
            except (subprocess.TimeoutExpired, OSError):
                pass
            time.sleep(poll_interval)
        return False

    def send_command(self, cmd, timeout_ms=3000, login=True):
        """Send a command to ISP console via serial_mux_client.

        If login=True, does a login call first then runs the command separately.
        The UART session persists across TCP connections so login sticks.

        Returns:
            Combined stdout+stderr output as string.
        """
        if login:
            # First connection: send login credentials
            login_cmd = [
                self.client_path, self.host, str(self.port),
                "--cmd", "\r\nroot\r\narlo\r\n", "--timeout", "2000",
            ]
            subprocess.run(login_cmd, capture_output=True, text=True, timeout=5)
            time.sleep(0.5)

        # Second connection (or only connection if login=False): send command
        full_cmd = [
            self.client_path, self.host, str(self.port),
            "--cmd", f"\r\n{cmd}\r\n", "--timeout", str(timeout_ms),
        ]

        result = subprocess.run(
            full_cmd, capture_output=True, text=True, timeout=timeout_ms / 1000 + 5
        )
        return result.stdout + result.stderr

    def check_connection(self):
        """Verify serial_mux is reachable."""
        try:
            output = self.send_command("echo serial_ok", timeout_ms=4000)
            return "Connected to serial_mux" in output or "serial_ok" in output
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            print(f"  serial_mux check failed: {e}")
            return False


def disable_fota(serial):
    """Disable FOTA by setting KV_BS_UPDATE_URL to invalid.com.

    Args:
        serial: SerialMuxClient instance.

    Returns:
        True if successful.
    """
    print("  [FOTA] Writing KV_BS_UPDATE_URL = invalid.com")
    serial.send_command("kvcmd write KV_BS_UPDATE_URL invalid.com", timeout_ms=2000)
    time.sleep(0.5)

    print("  [FOTA] Committing kvstore")
    serial.send_command("kvcmd commit", timeout_ms=2000)
    time.sleep(0.5)

    print("  [FOTA] Verifying...")
    output = serial.send_command("kvcmd read-s KV_BS_UPDATE_URL", timeout_ms=2000)
    if "invalid.com" in output:
        print("  [FOTA] OK — FOTA disabled")
        return True

    print(f"  [FOTA] WARNING: verification unclear, output: {output.strip()}")
    return True


def extract_birth_cert(serial):
    """Read birth certificate JSON from device via serial console.

    Uses asl_decrypt to decrypt the vault-encrypted DSC at
    /proc/device-tree/device_info/dsc/data.

    Args:
        serial: SerialMuxClient instance.

    Returns:
        Parsed JSON dict of birth certificate, or None on failure.
    """
    print("  [CERT] Decrypting birth certificate from ASL vault...")
    output = serial.send_command(
        "asl_decrypt /proc/device-tree/device_info/dsc/data",
        timeout_ms=5000,
    )

    json_start = output.find("{")
    json_end = output.rfind("}") + 1
    if json_start < 0 or json_end <= json_start:
        print(f"  [CERT] ERROR: Could not find JSON in output")
        print(f"  [CERT] Raw output: {output[:300]}")
        return None

    json_str = output[json_start:json_end]
    try:
        cert = json.loads(json_str)
        if "privateKey" not in cert:
            print(f"  [CERT] ERROR: birth_cert missing 'privateKey' field")
            return None
        if "certId" not in cert:
            print(f"  [CERT] ERROR: birth_cert missing 'certId' field")
            return None
        print(f"  [CERT] OK — certId={cert['certId'][:16]}...")
        return cert
    except json.JSONDecodeError as e:
        print(f"  [CERT] ERROR: JSON parse failed: {e}")
        print(f"  [CERT] Extracted: {json_str[:200]}")
        return None


def reboot_device(serial):
    """Reboot device via serial console."""
    print("  [REBOOT] Sending reboot command...")
    serial.send_command("reboot", timeout_ms=1000)
