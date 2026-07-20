# BLE Onboarding Automation Script — Implementation Plan

## Overview

Automated script that uses a Linux BT dongle to onboard an Arlo DUT (Lory doorbell) via BLE.
The script will replicate the Arlo app's onboarding flow: connect, exchange crypto keys,
pass WiFi credentials, trigger WiFi connection, send discovery token, and monitor until claimed.

---

## BLE Protocol Summary

### Service UUID

| Service | UUID |
|---------|------|
| **Arlo WiFi Service** | `f52be440-fd7a-11e5-92bd-0002a5d5c51b` |
| Battery Service | (secondary, not required for onboarding) |

### Characteristics (WiFi Service)

| # | Name | UUID | Perm | Format |
|---|------|------|------|--------|
| 0 | Info | `9fa15c68-cfe3-4e08-b85a-c31b0117ced5` | R | `{version:u8, capabilities:u8, cert_id:u8[16]}` (v1) |
| 1 | Mode | `1349a079-d6a4-4222-8e2a-ba5fa3e7f90b` | W | `u8` (v1) or `u16 BE` (v2) |
| 2 | Commissioner Key | `8ac4a1a4-c991-42e9-9c89-d709fe28e4aa` | W | 65 bytes (uncompressed EC P-256 public key) |
| 3 | Command | `56972ec0-fd8e-11e5-a8e6-0002a5d5c51b` | W | `u16 BE` (1=scan, 2=pair, 3=cancel, 4=wps) |
| 4 | Status & Error | `8e91e940-fd7b-11e5-874d-0002a5d5c51b` | R/Indicate | `{status:u16 BE, error:u16 BE}` |
| 5 | WiFi Mode | `de9ffda0-fd7b-11e5-b872-0002a5d5c51b` | W | `{band:u8, security:u8}` |
| 6 | WiFi SSID | `f8ac14e0-fd7b-11e5-a056-0002a5d5c51b` | W | AES-256-CBC encrypted (see Crypto section) |
| 7 | WiFi Passphrase | `1ac29540-fd7c-11e5-aaf3-0002a5d5c51b` | W | AES-256-CBC encrypted |
| 8 | Discovery Token | `376cc08b-e809-4faa-8864-4a24cd245337` | W | AES-256-CBC encrypted (8 bytes plaintext) |
| 9 | WiFi Scan Results | `6bfc9400-1097-11e6-92ec-0002a5d5c51b` | R | Binary scan list (paginated, 512B pages) |
| 10 | Claiming Status | `a55d4444-6cb9-4caa-bde0-0b447d1a7d7c` | R/Indicate | `u8` bitfield |
| 11 | FW Version | `640dfb6c-1717-4761-9c35-a7b682cda129` | R | String (v2 only) |
| 12 | Capabilities | `b224faba-314a-4cfa-8552-9131b2208499` | R | `u32 BE` (v2 only) |
| 13 | Certificate ID | `65958408-defc-4da8-a38e-3325157496b1` | R | `u8[16]` (v2 only) |
| 14 | Saved Networks | `e5821b04-396c-40d2-a796-b036a0cf02fd` | R | List of SSIDs (v2 only) |
| 15 | Remove Network | `9d0ebac6-da0e-41d9-83ac-9967f39f59cc` | W | Encrypted SSID (v2 only) |
| 16 | Quick Scan Results | `10e6b383-1436-415f-bdf6-2d375451c994` | R | Same as #9 (v2 only) |
| 17 | WiFi Security | `66be2424-797b-46a1-ba73-4c332345782e` | R | `u32 BE` supported security bitmask (v2 only) |
| 18 | WiFi Country Code | `6a44b7b0-e460-11ed-b5ea-0242ac120002` | W | 2 bytes, country code for WiFi regulation |
| 19 | User Language | `16cfae81-43e1-43e2-b338-da4dd8ca71e4` | W | 2 bytes, user language preference |

---

## Crypto Protocol (ECDH + AES-256-CBC)

The SSID, passphrase, and discovery token are encrypted before writing.

### Key Exchange

1. Device has a **birth certificate** with an EC P-256 private key (stored in ASL Vault)
2. The device's **Certificate ID** (16 bytes, hex-encoded = 32 chars) identifies which public key to use
3. Client (our script) generates an **ephemeral EC P-256 key pair**
4. Client writes its **uncompressed public key** (65 bytes: 0x04 || X || Y) to Commissioner Key characteristic
5. Device computes ECDH shared secret using its private key + client's public key
6. Shared secret (32 bytes raw) is base64-encoded → used as password for PBKDF2

### Encryption (for SSID, passphrase, discovery token)

Format: `"Salted__" || salt[8] || AES-256-CBC(plaintext)`

1. Generate random 8-byte salt
2. Derive key material via PBKDF2-HMAC-SHA256:
   - Password: base64(ECDH shared secret)
   - Salt: the 8-byte salt
   - Iterations: 10000
   - Output: 48 bytes (first 32 = AES key, last 16 = IV)
3. Encrypt plaintext with AES-256-CBC using PKCS#7 padding
4. Prepend `"Salted__"` (8 bytes) + salt (8 bytes) to ciphertext

### Getting the Device's Public Key

The script needs the **device's EC P-256 public key** (from its birth certificate) to verify identity.
In practice, the Arlo app fetches this from the cloud using the Certificate ID.

**For our test script:** We can either:
- (a) Extract the public key from the device's birth cert file (if accessible on-device via SSH)
- (b) Skip verification and just use our ephemeral key pair (the device only needs our public key)
- (c) Pre-extract and pass as argument

Since we're the client writing our public key, and the device does the ECDH with *its* private key,
we need the **device's public key** to compute the same shared secret on our side.

**Solution:** Read the device's birth certificate public key from the device over SSH before BLE pairing,
OR get it from the Arlo cloud API. For a dev/test flow, SSH extraction is simplest.

---

## Onboarding Sequence (Strict Order)

### Prerequisites
1. DUT is factory-reset (unclaimed)
2. serial_mux running (ISP port on localhost:9001)
3. BT dongle is available on the host

### Step-by-Step Protocol

```
Phase 0: Pre-BLE Setup (via serial_mux)
  0a. Disable FOTA: kvcmd write KV_BS_UPDATE_URL invalid.com && kvcmd commit
      Prevents device from auto-updating to production firmware during onboarding.
  0b. Extract device birth certificate public key:
      cat /etc/asl/birth_cert.json → parse JSON → extract EC P-256 public key
      This gives us the key needed for ECDH shared secret computation.
  0c. (DUT reboot if FOTA URL was changed — needed for kvcmd to take effect)
  0d. Put DUT in BLE advertising mode (press SYNC button, or trigger via serial)

Phase 1: BLE Connection
  1. Scan for BLE device advertising as "AVD6001" (or model ID)
  2. Connect to device (GATT connection, no bonding required)
  3. Discover services — find WiFi Service UUID

Phase 2: Read Device Info
  4. Read "Info" characteristic → get version, capabilities, cert_id
  5. (v2) Read "Capabilities" → verify D2AP_WIFI is supported (bit 1)
  6. (v2) Read "Certificate ID" → get cert_id for key lookup
  7. (v2) Read "FW Version" → informational

Phase 3: Key Exchange
  8. Generate ephemeral EC P-256 key pair (secp256r1)
  9. Write 65-byte uncompressed public key to "Commissioner Key"
     Device computes shared secret internally.
  10. Compute shared secret on our side: ECDH(our_private, device_public)
      → base64 encode → this is the encryption password

Phase 4: Set Onboarding Mode
  11. Write mode to "Mode" characteristic:
      - v1: u8 value 2 (ONBOARDING_MODE_D2AP_WIFI)
      - v2: u16 BE value 2
      Device subscribes to claiming events internally.

Phase 5: WiFi Configuration
  12. Write "WiFi Mode": {band, security}
      - band: 0=not specified, 1=2.4GHz, 2=5GHz
      - security: 0=not specified, 2=WPA2, 3=WPA3, 11=WPA2/WPA3
  13. Write encrypted SSID to "WiFi SSID"
  14. Write encrypted passphrase to "WiFi Passphrase"

Phase 6: Trigger WiFi Connection
  15. Write command 2 (BLE_CMD_PAIR) to "Command" characteristic
      Device begins WiFi connection attempts (up to 3 retries with WPA3→WPA2 fallback)
  16. Subscribe to "Status & Error" indications
  17. Wait for status to include ONBOARDING_STATUS_PAIRED (bit 3 = 0x0008)
      - If ONBOARDING_STATUS_ERROR (bit 15), read error code and abort

Phase 7: Cloud Discovery
  18. Generate random 8-byte discovery token
  19. Write encrypted discovery token to "Discovery Token"
      Device sends token to xAgent → xAgent pushes to xCloud
      Device enters CLAIMING state
  20. Subscribe to "Claiming Status" indications
  21. Monitor claiming status bits:
      - bit 6: XAGENT_PREREGISTERED
      - bit 0: XAGENT_REGISTERED
      - bit 1: XAGENT_DISCOVERY_DATA_PUSHED
      - bit 2: XAGENT_CONNECTED
      - bit 3: XAGENT_CLAIMED
      - bit 4: DEVICE_CLAIMED ← final success

Phase 8: Cloud Claim (via Arlo hmsweb API)
  22. Authenticate with Arlo cloud (same Playwright+Cloudflare bypass as livestream test)
      POST auth_api/ocapi/accounts/v1/auth → get token
      GET hmsweb_api/hmsweb/users/session/v3 → establish session
  23. Poll hmsweb_api/hmsweb/v2/users/devices until our device appears
      (device becomes visible after xAgent discovery push with our token)
  24. Claim the device via hmsweb API using discovery token correlation
  25. Monitor "Claiming Status" BLE characteristic for DEVICE_CLAIMED (bit 4 = 0x10)

Phase 9: Completion
  26. When DEVICE_CLAIMED is set → onboarding succeeded
  27. Disconnect BLE
  28. Report claim code (available via kvcmd read-s x_agent_claim_code on device via serial)
```

---

## Status & Error Decoding

### Status Bits (u16)

| Bit | Mask | Meaning |
|-----|------|---------|
| 0 | 0x0001 | SCANNING |
| 1 | 0x0002 | SCAN_COMPLETED |
| 2 | 0x0004 | PAIRING (WiFi connecting) |
| 3 | 0x0008 | PAIRED (WiFi connected, NTP synced) |
| 4 | 0x0010 | CLAIMING_D2AP |
| 5 | 0x0020 | CLAIMED_D2AP |
| 6 | 0x0040 | CLAIMING_D2BS |
| 7 | 0x0080 | CLAIMED_D2BS |
| 8 | 0x0100 | QSCAN_COMPLETED |
| 14 | 0x4000 | NO_INTERNET |
| 15 | 0x8000 | ERROR |

### Claiming Status Bits (u8)

| Bit | Mask | Meaning |
|-----|------|---------|
| 0 | 0x01 | XAGENT_REGISTERED |
| 1 | 0x02 | XAGENT_DISCOVERY_DATA_PUSHED |
| 2 | 0x04 | XAGENT_CONNECTED |
| 3 | 0x08 | XAGENT_CLAIMED |
| 4 | 0x10 | DEVICE_CLAIMED |
| 5 | 0x20 | BS_REGISTERED_CONFIRM |
| 6 | 0x40 | XAGENT_PREREGISTERED |

---

## WiFi Scan Results Format (for optional scan step)

```
struct WiFiScanResults {
    u16 total_size;       // big-endian
    u16 count;            // big-endian
    struct Entry {
        u8 band;          // 1=2.4GHz, 2=5GHz
        u8 encryption;    // see security enum below
        i16 rssi;         // big-endian, dBm
        u8 ssid_len;
        u8 ssid[ssid_len];
    } entries[count];
};
```

---

## Script Arguments

### Required
| Argument | Description |
|----------|-------------|
| `--ssid` | WiFi network SSID |
| `--psk` | WiFi passphrase |

### Optional
| Argument | Default | Description |
|----------|---------|-------------|
| `--device-name` | `AVD6001` | BLE advertising name to scan for |
| `--device-mac` | (auto-scan) | Skip scan, connect to specific MAC |
| `--band` | `0` (auto) | WiFi band: 0=auto, 1=2.4GHz, 2=5GHz |
| `--security` | `0` (auto) | WiFi security: 0=auto, 2=WPA2, 3=WPA3, 11=WPA2/WPA3 |
| `--device-pubkey` | (extract via serial) | Path to device's EC P-256 public key PEM file (overrides serial extraction) |
| `--config` | `ble_onboard.ini` | Path to config file (see .ini.template) |
| `--bt-adapter` | (system default) | Bluetooth adapter to use (e.g. `hci0`, `hci1`) |
| `--serial-host` | `localhost` | serial_mux host |
| `--serial-port` | `9001` | serial_mux ISP port |
| `--no-fota-disable` | False | Skip the KV_BS_UPDATE_URL disable step |
| `--timeout` | `180` | Total timeout in seconds |
| `--scan-wifi` | False | Perform WiFi scan before connecting (informational) |
| `--mode` | `2` | Onboarding mode (2=D2AP_WIFI) |
| `--skip-discovery` | False | Skip cloud discovery/claiming phase (for WiFi-only test) |
| `--verbose` | False | Print all BLE exchanges |

---

## Implementation Plan

### Technology Stack
- **Python 3** (matches existing test scripts in `device_tests/`)
- **bleak** library for cross-platform BLE GATT client (pip install bleak)
- **cryptography** or **mbedtls** bindings for ECDH + AES
- Standard library: `hashlib`, `os`, `struct`, `asyncio`

### File Structure
```
utils/custom/device_tests/
├── ble_onboard.py              # Main script
├── ble_onboard_crypto.py       # ECDH key exchange + AES encryption helpers
├── ble_onboard_constants.py    # UUIDs, status codes, error codes
├── ble_onboard_serial.py       # serial_mux helpers (FOTA disable, pubkey extraction)
├── ble_onboard_cloud.py        # Arlo cloud auth + claim API (Playwright-based)
├── ble_onboard.ini.template    # Config template — copy to ble_onboard.ini and fill in
└── ble_onboard_plan.md         # This file
```

### Implementation Steps

1. **`ble_onboard_constants.py`** — Define all UUIDs, status/error enums, modes
2. **`ble_onboard_crypto.py`** — Implement:
   - `generate_keypair()` → (private_key, public_key_65bytes)
   - `compute_shared_secret(our_private, device_public)` → base64 string
   - `encrypt_value(shared_secret_b64, plaintext_bytes)` → encrypted bytes
   - `extract_device_pubkey_ssh(ip, password)` → public key bytes (optional helper)
3. **`ble_onboard.py`** — Main async script:
   - Argument parsing
   - BLE scan + connect
   - GATT service/characteristic discovery
   - Execute onboarding sequence (see above)
   - Status polling via indications or periodic reads
   - Error handling + timeout logic
   - Import `mcu_patterns.py` for anomaly detection if serial is available

### Key Design Decisions

1. **Device public key acquisition:**
   - **Primary:** Read birth cert from device via serial_mux ISP console before pressing SYNC
     (`cat /etc/asl/birth_cert.json` via serial_mux_client → extract public key)
   - **Fallback:** If serial extraction fails, prompt user to provide `--device-pubkey`
   - **Note:** Without the device's public key, we CANNOT compute the shared secret
     and encrypt values. Serial access solves this cleanly — no SSH/WiFi needed pre-onboard.

1b. **FOTA disable (Phase 0):**
   - Before onboarding, set `KV_BS_UPDATE_URL` to `invalid.com` via serial_mux
   - Prevents device from auto-updating to production firmware mid-onboarding
   - Requires reboot after kvcmd commit for the setting to take effect
   - Skip with `--no-fota-disable` if already done or running production firmware

2. **BLE pairing level:** No BLE-level bonding or authentication is required.
   MCU uses `BTM_IO_CAPABILITIES_NONE` (Just Works). Security is application-level
   via ECDH Commissioner Key exchange. GATT attributes have no auth requirement.

3. **Long writes:** Commissioner Key (65B), WiFi SSID (64B), WiFi Passphrase (96B),
   Discovery Token (32B) use GATT Reliable Write (Prepare Write + Execute Write).
   The `bleak` library handles this automatically when the value exceeds ATT MTU.

4. **Async architecture:** Use `bleak` with asyncio for non-blocking BLE operations
   and indication handling.

5. **Error recovery:** If WiFi connection fails (status shows ERROR), the script can:
   - Read the error code, report it, and optionally retry with different band/security

6. **Discovery token:** Generate a random 8-byte token. The device converts it to hex
   string (16 chars) and sends it to xAgent via JSON:
   `{"command":"send_discovery","service":"1002","id":"<hex_token>"}`

---

## BLE Advertising & Scan Identification

The device advertises with:
- **Complete Local Name:** Model ID (e.g., `"AVD6001"` for Lory 2K)
- **Manufacturer Specific Data:** Company ID `0x0c19` (Arlo Technologies)
- **High duty:** 30ms intervals for first 60 seconds
- **Low duty:** 1280ms intervals indefinitely after
- **TX Power:** -10 dBm (short range for security — be close to device)

To find the DUT: scan for devices with name matching `"AVD6001"` or `"AVD5001"` (Lory FHD),
or filter by manufacturer company ID `0x0c19`.

---

## Risks and Open Questions

1. **Device public key:** ~~How to reliably get the device's EC public key?~~
   **RESOLVED:** Extract via serial_mux (always available). Read `/etc/asl/birth_cert.json`
   over ISP console before starting BLE flow.

2. **BLE advertising duration:** After SYNC press, device advertises for ~120s (v1) or
   ~300s (v2). Script must connect within this window. Phase 0 (serial setup) must
   complete before pressing SYNC — the script should prompt user when ready.

3. **Claiming timeout:** The device has a 70-second claiming timer. If cloud registration
   doesn't complete in time, onboarding fails.

4. **v1 vs v2 protocol:** Lory uses ONBOARDING_V2 (BLE_VER_4_BLE_V2). The mode
   characteristic is u16 big-endian and additional characteristics are available.

5. **FOTA race:** If device boots and immediately starts FOTA check before kvcmd takes
   effect, there's a small window. Mitigation: disable FOTA before factory-reset, or
   ensure the device has no cloud connectivity until WiFi credentials are provided via BLE.

---

## Cloud Claim Strategy

The claim step requires the **user-side** (app/script) to tell the Arlo cloud to associate the device
with a user account. The device side pushes a discovery token to xCloud's presence service;
the app side claims the device by correlating that same token.

### Authentication (reuse livestream_cycle_test.py pattern)
- Use Playwright (headless Chromium) to bypass Cloudflare challenge on goldendev
- POST `auth_api/ocapi/accounts/v1/auth` with email+password → get `token`
- GET `hmsweb_api/hmsweb/users/session/v3` with `Authorization: <token>` → establish session

### Claim Flow (app-side)
1. After BLE Phase 7 (discovery token pushed), the device appears on the cloud
2. Poll `hmsweb_api/hmsweb/v2/users/devices` until a new unclaimed device appears
   (match by hardware ID / serial number read from device info or birth cert)
3. Claim the device — likely `POST hmsweb_api/hmsweb/users/devices/adopt` or similar endpoint
   with the discovery token we generated
4. Once claimed, the cloud notifies the device → DEVICE_CLAIMED bit set

### Configuration
All cloud credentials stored in `ble_onboard.ini` (copied from `.ini.template`).
The template is pre-filled with goldendev endpoints and the existing test account.

### Note on Playwright Dependency
Playwright + Chromium is required for Cloudflare bypass (same as livestream test).
Install: `pip install playwright && playwright install chromium`

---

## Dependencies

```
pip install bleak cryptography playwright websockets
playwright install chromium
```

- `bleak>=0.21.0` — BLE GATT client
- `cryptography>=41.0` — ECDH (secp256r1), AES-256-CBC, PBKDF2
- `playwright` — Cloudflare bypass for Arlo cloud auth
- `websockets` — optional, for future extensions

---

## Usage Example

```bash
# First time: copy template and fill in WiFi credentials
cp ble_onboard.ini.template ble_onboard.ini
# Edit ble_onboard.ini: set ssid, psk (cloud creds pre-filled for goldendev)

# Basic onboarding (serial_mux running, extracts pubkey + disables FOTA automatically)
python3 ble_onboard.py

# Override WiFi from command line (takes precedence over .ini)
python3 ble_onboard.py --ssid "MyWiFi" --psk "password123"

# Use specific BT adapter (e.g. USB dongle on hci1)
python3 ble_onboard.py --bt-adapter hci1

# With explicit device MAC (skip BLE scan)
python3 ble_onboard.py --device-mac AA:BB:CC:DD:EE:FF

# WiFi-only test (skip cloud claiming)
python3 ble_onboard.py --skip-discovery

# With WiFi scan step and explicit band/security
python3 ble_onboard.py --scan-wifi --band 1 --security 2

# FOTA already disabled, provide pubkey manually
python3 ble_onboard.py --no-fota-disable --device-pubkey ./device_pub.pem

# Custom config path
python3 ble_onboard.py --config /path/to/my_config.ini
```
