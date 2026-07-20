"""BLE Onboarding Constants — UUIDs, status codes, error codes."""

# WiFi Service UUID
WIFI_SERVICE_UUID = "f52be440-fd7a-11e5-92bd-0002a5d5c51b"

# Characteristic UUIDs (WiFi Service)
CHAR_INFO = "9fa15c68-cfe3-4e08-b85a-c31b0117ced5"
CHAR_MODE = "1349a079-d6a4-4222-8e2a-ba5fa3e7f90b"
CHAR_COMMISSIONER_KEY = "8ac4a1a4-c991-42e9-9c89-d709fe28e4aa"
CHAR_COMMAND = "56972ec0-fd8e-11e5-a8e6-0002a5d5c51b"
CHAR_STATUS_ERROR = "8e91e940-fd7b-11e5-874d-0002a5d5c51b"
CHAR_WIFI_MODE = "de9ffda0-fd7b-11e5-b872-0002a5d5c51b"
CHAR_WIFI_SSID = "f8ac14e0-fd7b-11e5-a056-0002a5d5c51b"
CHAR_WIFI_PASSPHRASE = "1ac29540-fd7c-11e5-aaf3-0002a5d5c51b"
CHAR_DISCOVERY_TOKEN = "376cc08b-e809-4faa-8864-4a24cd245337"
CHAR_WIFI_SCAN_RESULTS = "6bfc9400-1097-11e6-92ec-0002a5d5c51b"
CHAR_CLAIMING_STATUS = "a55d4444-6cb9-4caa-bde0-0b447d1a7d7c"
CHAR_FW_VERSION = "640dfb6c-1717-4761-9c35-a7b682cda129"
CHAR_CAPABILITIES = "b224faba-314a-4cfa-8552-9131b2208499"
CHAR_CERTIFICATE_ID = "65958408-defc-4da8-a38e-3325157496b1"
CHAR_SAVED_NETWORKS = "e5821b04-396c-40d2-a796-b036a0cf02fd"
CHAR_REMOVE_NETWORK = "9d0ebac6-da0e-41d9-83ac-9967f39f59cc"
CHAR_QUICK_SCAN_RESULTS = "10e6b383-1436-415f-bdf6-2d375451c994"
CHAR_WIFI_SECURITY = "66be2424-797b-46a1-ba73-4c332345782e"

# BLE Commands (write to CHAR_COMMAND as u16 BE)
BLE_CMD_SCAN = 1
BLE_CMD_PAIR = 2
BLE_CMD_CANCEL = 3
BLE_CMD_WPS = 4

# Onboarding Modes (write to CHAR_MODE)
ONBOARDING_MODE_UNDEFINED = 0
ONBOARDING_MODE_D2BS = 1
ONBOARDING_MODE_D2AP_WIFI = 2
ONBOARDING_MODE_D2AP_POE = 3
ONBOARDING_MODE_RE_D2BS = 4
ONBOARDING_MODE_RE_D2AP = 5

# Status bits (u16, from CHAR_STATUS_ERROR high byte)
STATUS_SCANNING = 0x0001
STATUS_SCAN_COMPLETED = 0x0002
STATUS_PAIRING = 0x0004
STATUS_PAIRED = 0x0008
STATUS_CLAIMING_D2AP = 0x0010
STATUS_CLAIMED_D2AP = 0x0020
STATUS_CLAIMING_D2BS = 0x0040
STATUS_CLAIMED_D2BS = 0x0080
STATUS_QSCAN_COMPLETED = 0x0100
STATUS_NO_INTERNET = 0x4000
STATUS_ERROR = 0x8000

# Claiming status bits (u8, from CHAR_CLAIMING_STATUS)
CLAIMING_REGISTERED = 0x01
CLAIMING_DISCOVERY_PUSHED = 0x02
CLAIMING_CONNECTED = 0x04
CLAIMING_CLAIMED = 0x08
CLAIMING_DEVICE_CLAIMED = 0x10
CLAIMING_BS_REGISTERED = 0x20
CLAIMING_PREREGISTERED = 0x40

# WiFi bands
WIFI_BAND_AUTO = 0
WIFI_BAND_2_4GHZ = 1
WIFI_BAND_5GHZ = 2

# WiFi security types
WIFI_SEC_AUTO = 0
WIFI_SEC_OPEN = 1
WIFI_SEC_WPA2 = 2
WIFI_SEC_WPA3 = 3
WIFI_SEC_WPA2_WPA3 = 11

# Protocol versions
BLE_VER_3_ERRORS = 3
BLE_VER_4_BLE_V2 = 4

# Crypto constants
COMMISSIONER_KEY_SIZE = 65
DISCOVERY_TOKEN_SIZE = 8
CRYPTO_CERT_ID_SIZE = 16
ECDH_SHARED_SECRET_SIZE = 32
PBKDF2_ITERATIONS = 10000
PBKDF2_OUTPUT_SIZE = 48  # 32 key + 16 IV

# BLE manufacturer ID
ARLO_COMPANY_ID = 0x0C19

# Error code ranges
ERROR_WIFI_BASE = 0x1000
ERROR_ETHERNET_BASE = 0x2000
ERROR_NETWORK_BASE = 0x3000
ERROR_XAGENT_BASE = 0x4000
ERROR_CLAIMING_BASE = 0x5000
ERROR_STORE_BASE = 0x6000


def status_to_str(status):
    """Decode status bitmask to human-readable string."""
    flags = []
    if status & STATUS_SCANNING:
        flags.append("SCANNING")
    if status & STATUS_SCAN_COMPLETED:
        flags.append("SCAN_DONE")
    if status & STATUS_PAIRING:
        flags.append("PAIRING")
    if status & STATUS_PAIRED:
        flags.append("PAIRED")
    if status & STATUS_CLAIMING_D2AP:
        flags.append("CLAIMING")
    if status & STATUS_CLAIMED_D2AP:
        flags.append("CLAIMED")
    if status & STATUS_QSCAN_COMPLETED:
        flags.append("QSCAN_DONE")
    if status & STATUS_NO_INTERNET:
        flags.append("NO_INTERNET")
    if status & STATUS_ERROR:
        flags.append("ERROR")
    return "|".join(flags) if flags else "IDLE"


def claiming_to_str(claiming):
    """Decode claiming status bitmask to human-readable string."""
    flags = []
    if claiming & CLAIMING_PREREGISTERED:
        flags.append("PREREG")
    if claiming & CLAIMING_REGISTERED:
        flags.append("REGISTERED")
    if claiming & CLAIMING_DISCOVERY_PUSHED:
        flags.append("DISCOVERY_PUSHED")
    if claiming & CLAIMING_CONNECTED:
        flags.append("CONNECTED")
    if claiming & CLAIMING_CLAIMED:
        flags.append("CLAIMED")
    if claiming & CLAIMING_DEVICE_CLAIMED:
        flags.append("DEVICE_CLAIMED")
    return "|".join(flags) if flags else "NONE"


def error_to_str(error):
    """Decode error code to category string."""
    if error == 0:
        return "NONE"
    if error >= ERROR_STORE_BASE:
        return f"STORE(0x{error:04x})"
    if error >= ERROR_CLAIMING_BASE:
        return f"CLAIMING(0x{error:04x})"
    if error >= ERROR_XAGENT_BASE:
        return f"XAGENT(0x{error:04x})"
    if error >= ERROR_NETWORK_BASE:
        return f"NETWORK(0x{error:04x})"
    if error >= ERROR_ETHERNET_BASE:
        return f"ETHERNET(0x{error:04x})"
    if error >= ERROR_WIFI_BASE:
        return f"WIFI(0x{error:04x})"
    return f"UNKNOWN(0x{error:04x})"
