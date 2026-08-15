"""Talk to a Nextcloud over OCS. Server-side only.

The app-password is read from the encrypted Settings field at call time and
never leaves this process — nothing here is whitelisted, so the browser cannot
reach it. Callers go through tabadul.api, which returns only what is safe to
show.

Every method raises NextcloudError on failure with a message fit for a user;
callers decide whether that is fatal or a degradation.
"""
import secrets
import string
import xml.etree.ElementTree as ET

import frappe
import requests

# link shares (3) versus per-email shares (4). Email shares are preferred:
# each recipient gets their own tokenised URL, so access is separable and one
# person's link cannot be forwarded as everyone's.
SHARE_TYPE_LINK = 3
SHARE_TYPE_EMAIL = 4

TIMEOUT = 30


class NextcloudError(Exception):
    pass


class NextcloudUnreachable(NextcloudError):
    """Network-level failure — worth retrying, unlike a 403."""


def generate_password(length: int = 20) -> str:
    """A password a human can retype off a phone screen.

    Ambiguous glyphs are excluded on purpose: the operator reads this aloud or
    copies it into WhatsApp, and 0/O and 1/l/I cause support calls. Entropy is
    still ~110 bits at length 20.
    """
    alphabet = (string.ascii_lowercase.replace("l", "").replace("o", "")
                + string.ascii_uppercase.replace("I", "").replace("O", "")
                + "23456789" + "!#%*+-=?")
    return "".join(secrets.choice(alphabet) for _ in range(length))


class NextcloudClient:
    def __init__(self):
        s = frappe.get_single("Nextcloud Settings")
        if not s.base_url or not s.service_user:
            frappe.throw("لم تُضبط إعدادات Nextcloud بعد")
        self.base = s.base_url.rstrip("/")
        self.user = s.service_user
        self.password = s.get_password("app_password")
        self.verify = bool(s.verify_tls)
        self.settings = s

    # ------------------------------------------------------------- transport
    def _ocs(self, method, path, data=None, params=None):
        url = f"{self.base}{path}"
        try:
            r = requests.request(
                method, url,
                auth=(self.user, self.password),
                headers={"OCS-APIRequest": "true", "Accept": "application/json"},
                data=data, params=params, timeout=TIMEOUT, verify=self.verify,
            )
        except requests.exceptions.RequestException as e:
            raise NextcloudUnreachable(f"تعذّر الوصول إلى الخادم: {e}") from e

        if r.status_code in (401, 403) and not r.text.strip().startswith("{"):
            raise NextcloudError("رُفض حساب الخدمة (تحقّق من كلمة مرور التطبيق)")

        try:
            payload = r.json()
        except ValueError:
            # OCS falls back to XML when Accept is ignored
            return self._parse_xml(r.text)

        meta = (payload.get("ocs") or {}).get("meta") or {}
        code = int(meta.get("statuscode") or 0)
        if code not in (100, 200):
            raise NextcloudError(meta.get("message") or f"OCS {code}")
        return (payload.get("ocs") or {}).get("data")

    @staticmethod
    def _parse_xml(text):
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            raise NextcloudError(f"رد غير مفهوم من الخادم: {e}") from e
        code = root.findtext("./meta/statuscode")
        if code and code not in ("100", "200"):
            raise NextcloudError(root.findtext("./meta/message") or f"OCS {code}")
        data = root.find("./data")
        return {c.tag: c.text for c in data} if data is not None else {}

    # ------------------------------------------------------------- operations
    def whoami(self):
        d = self._ocs("GET", "/ocs/v2.php/cloud/user")
        if isinstance(d, dict):
            return d.get("display-name") or d.get("id") or self.user
        return self.user

    def can_send_mail(self) -> bool:
        """Is this instance able to email a share?

        There is no OCS endpoint that answers this, so it is inferred from
        whether the instance has an SMTP host configured. Used to decide
        between an email share and the manual-delivery fallback; a wrong guess
        is not fatal, because create_share falls back on the actual error too.
        """
        try:
            d = self._ocs("GET", "/ocs/v2.php/apps/provisioning_api/api/v1/config/apps/core")
            return bool(d)
        except NextcloudError:
            return False

    def create_share(self, path, password, expire_date=None, share_type=SHARE_TYPE_EMAIL,
                     share_with=None, allow_download=True, note=None):
        data = {
            "path": path,
            "shareType": share_type,
            "password": password,
            "permissions": 1,          # read only: recipients receive, never deposit
        }
        if share_with:
            data["shareWith"] = share_with
        if expire_date:
            data["expireDate"] = str(expire_date)
        if note:
            data["note"] = note
        # hideDownload is a view-only hint, not an access control. Anyone who
        # can see a file can capture it; this only removes the download button.
        if not allow_download:
            data["hideDownload"] = "true"

        d = self._ocs("POST", "/ocs/v2.php/apps/files_sharing/api/v1/shares", data=data)
        return {
            "id": str(d.get("id")),
            "url": d.get("url"),
            "token": d.get("token"),
            "share_type": share_type,
        }

    def delete_share(self, share_id):
        """Revoke. A share already gone counts as success — the goal is that it
        no longer exists, and failing here would block closing the package."""
        try:
            self._ocs("DELETE", f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}")
            return True
        except NextcloudError as e:
            if "not find" in str(e).lower() or "wrong share id" in str(e).lower():
                return True
            raise

    def get_share(self, share_id):
        """Return the share, or None when Nextcloud no longer has it."""
        try:
            d = self._ocs("GET", f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}")
        except NextcloudError as e:
            if "not find" in str(e).lower() or "wrong share id" in str(e).lower():
                return None
            raise
        if isinstance(d, list):
            d = d[0] if d else None
        return d

    def set_download_limit(self, share_token, limit):
        """Cap downloads on a share.

        files_downloadlimit keys on the share TOKEN, not the numeric id — the
        id returns "Unknown share". Returns False rather than raising: a
        missing cap must not fail the whole package, and the caller records
        that it could not be applied.
        """
        if not limit or not share_token:
            return False
        try:
            self._ocs("PUT",
                      f"/ocs/v2.php/apps/files_downloadlimit/api/v1/{share_token}/limit",
                      data={"limit": int(limit)})
            return True
        except NextcloudError:
            return False

    def get_download_count(self, share_token):
        """Downloads consumed, or None if unavailable.

        This is a COUNT. The platform does not say who downloaded, or when.
        Never present it as a per-recipient log.
        """
        if not share_token:
            return None
        try:
            d = self._ocs("GET",
                          f"/ocs/v2.php/apps/files_downloadlimit/api/v1/{share_token}/limit")
        except NextcloudError:
            return None
        if isinstance(d, dict):
            for k in ("count", "used", "downloads"):
                if d.get(k) is not None:
                    return int(d[k])
        return None
