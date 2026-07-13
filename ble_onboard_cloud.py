"""BLE Onboarding Cloud — Arlo cloud authentication and device claiming via Playwright."""

import time
import json

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class ArloCloudClient:
    """Arlo cloud API client using Playwright for Cloudflare bypass."""

    def __init__(self, email, password, auth_api, hmsweb_api, site_url):
        self.email = email
        self.password = password
        self.auth_api = auth_api
        self.hmsweb_api = hmsweb_api
        self.site_url = site_url
        self.pw = None
        self.browser = None
        self.page = None
        self.token = None

    def connect(self):
        """Launch browser, bypass Cloudflare, authenticate."""
        if sync_playwright is None:
            raise RuntimeError("playwright not installed: pip install playwright && playwright install chromium")

        print("  [CLOUD] Launching Playwright...")
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

        print("  [CLOUD] Navigating to Arlo site (CF challenge ~15s)...")
        self.page.goto(self.site_url, timeout=90000)
        time.sleep(15)

        print("  [CLOUD] Authenticating...")
        self._authenticate()

    def _authenticate(self):
        """Auth via ocapi and establish hmsweb session."""
        result = self.page.evaluate("""async ([authApi, hmsApi, email, password]) => {
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
                headers: {'auth-version': '2', 'Authorization': token,
                          'content-type': 'application/json; charset=utf-8',
                          'origin': arguments[0], 'x-transaction-id': sid},
                credentials: 'include'
            });
            if (sessResp.status !== 200) return {error: 'session failed', status: sessResp.status};

            return {token};
        }""", [self.auth_api, self.hmsweb_api, self.email, self.password])

        if result.get("error"):
            raise RuntimeError(f"Cloud auth failed: {result}")

        self.token = result["token"]
        print(f"  [CLOUD] Authenticated OK")

    def get_devices(self):
        """Get list of devices on the account."""
        result = self.page.evaluate("""async ([hmsApi, token, origin]) => {
            function txId() {
                return 'FE!' + ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
                    (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
            }
            const did = txId();
            const resp = await fetch(hmsApi + '/hmsweb/v2/users/devices?t=' + Date.now() + '&eventId=' + did + '&time=' + Date.now(), {
                headers: {'auth-version': '2', 'Authorization': token,
                          'content-type': 'application/json; charset=utf-8',
                          'origin': origin, 'x-transaction-id': did},
                credentials: 'include'
            });
            if (resp.status !== 200) return {error: 'devices fetch failed', status: resp.status};
            const data = await resp.json();
            return data;
        }""", [self.hmsweb_api, self.token, self.site_url])

        if result.get("error"):
            print(f"  [CLOUD] Devices fetch failed: {result}")
            return []
        return result.get("data", [])

    def claim_device(self, device_id, hardware_id):
        """Claim a device on the account.

        Tries multiple known claim endpoint patterns.
        """
        result = self.page.evaluate("""async ([hmsApi, token, origin, deviceId, hardwareId]) => {
            function txId() {
                return 'FE!' + ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
                    (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
            }

            // Try claiming via the adopt/claim endpoint
            const tid = txId();
            const body = {
                deviceId: deviceId,
                hardwareId: hardwareId,
                deviceName: 'Lory Doorbell'
            };

            // Attempt 1: POST to devices/claim
            let resp = await fetch(hmsApi + '/hmsweb/users/devices/claim', {
                method: 'POST',
                headers: {'auth-version': '2', 'Authorization': token,
                          'content-type': 'application/json; charset=utf-8',
                          'origin': origin, 'x-transaction-id': tid},
                credentials: 'include',
                body: JSON.stringify(body)
            });
            if (resp.status === 200) {
                const data = await resp.json();
                return {method: 'claim', status: resp.status, data};
            }

            // Attempt 2: POST to devices/adopt
            const tid2 = txId();
            resp = await fetch(hmsApi + '/hmsweb/users/devices/adopt', {
                method: 'POST',
                headers: {'auth-version': '2', 'Authorization': token,
                          'content-type': 'application/json; charset=utf-8',
                          'origin': origin, 'x-transaction-id': tid2},
                credentials: 'include',
                body: JSON.stringify(body)
            });
            if (resp.status === 200) {
                const data = await resp.json();
                return {method: 'adopt', status: resp.status, data};
            }

            // Attempt 3: PUT to devices/{deviceId}
            const tid3 = txId();
            resp = await fetch(hmsApi + '/hmsweb/users/devices/' + deviceId, {
                method: 'PUT',
                headers: {'auth-version': '2', 'Authorization': token,
                          'content-type': 'application/json; charset=utf-8',
                          'origin': origin, 'x-transaction-id': tid3},
                credentials: 'include',
                body: JSON.stringify({claimed: true, deviceName: 'Lory Doorbell'})
            });
            const putData = await resp.text();
            return {method: 'put_device', status: resp.status, data: putData.substring(0, 500)};
        }""", [self.hmsweb_api, self.token, self.site_url, device_id, hardware_id])

        return result

    def wait_for_device(self, hardware_id, timeout=90):
        """Poll device list until a device with matching hardware_id appears.

        Args:
            hardware_id: Device serial number to look for.
            timeout: Max seconds to wait.

        Returns:
            Device dict if found, None on timeout.
        """
        print(f"  [CLOUD] Waiting for device {hardware_id} to appear (timeout {timeout}s)...")
        start = time.time()
        poll_interval = 5

        while time.time() - start < timeout:
            devices = self.get_devices()
            for d in devices:
                hw = d.get("deviceId", "") or d.get("hardwareId", "")
                if hardware_id in hw or hw in hardware_id:
                    print(f"  [CLOUD] Device found: {d.get('deviceName', '?')} ({hw})")
                    return d
            time.sleep(poll_interval)

        print(f"  [CLOUD] Timeout waiting for device")
        return None

    def close(self):
        """Clean up browser resources."""
        if self.browser:
            self.browser.close()
        if self.pw:
            self.pw.stop()
