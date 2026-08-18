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
from frappe import _
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
    def __init__(self, settings=None):
        """Talk to the configured Nextcloud.

        ``settings`` may be an unsaved Nextcloud Settings document. That matters
        for testing a connection during save: the database still holds the OLD
        credentials at that point, so a check that read from it would validate
        the previous password and report success for credentials nobody is
        about to use.
        """
        s = settings or frappe.get_single("Nextcloud Settings")
        if not s.base_url or not s.service_user:
            frappe.throw(_("Nextcloud settings are not configured yet"))
        self.base = s.base_url.rstrip("/")
        self.user = s.service_user
        self.password = self._resolve_password(s)
        self.verify = bool(s.verify_tls)
        self.settings = s

    @staticmethod
    def _resolve_password(s):
        """Prefer a freshly typed password over the stored one.

        Frappe masks a saved Password field as asterisks when the document is
        loaded. An all-asterisk value is therefore the mask, not a password,
        and the real one has to come from the auth table.
        """
        raw = s.get("app_password")
        if raw and set(str(raw)) != {"*"}:
            return raw
        return s.get_password("app_password")

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
            raise NextcloudUnreachable(_("Could not reach the server: {0}").format(e)) from e

        if r.status_code in (401, 403) and not r.text.strip().startswith("{"):
            raise NextcloudError(_("The service account was rejected (check the app password)"))

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
            raise NextcloudError(_("Unreadable response from the server: {0}").format(e)) from e
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

    # ------------------------------------------------------------- WebDAV
    def _dav_url(self, remote_path):
        from urllib.parse import quote
        p = "/" + remote_path.strip("/")
        return f"{self.base}/remote.php/dav/files/{quote(self.user)}{quote(p)}"

    def ensure_folder(self, remote_path):
        """MKCOL each segment. 405 means it already exists, which is success."""
        parts = [p for p in remote_path.strip("/").split("/") if p]
        cur = ""
        for seg in parts:
            cur = f"{cur}/{seg}"
            try:
                r = requests.request("MKCOL", self._dav_url(cur),
                                     auth=(self.user, self.password),
                                     timeout=TIMEOUT, verify=self.verify)
            except requests.exceptions.RequestException as e:
                raise NextcloudUnreachable(_("Could not reach the server: {0}").format(e)) from e
            if r.status_code not in (201, 405):
                raise NextcloudError(_("Could not create folder {0} ({1})").format(cur, r.status_code))
        return True

    def upload_file(self, remote_path, content: bytes):
        """PUT bytes to a WebDAV path, creating parent folders first.

        Single PUT: the edge proxy cuts a request body at 60 seconds, so this
        suits documents, not multi-gigabyte media. Chunked upload belongs here
        if that limit is ever hit in practice.
        """
        folder = "/".join(remote_path.strip("/").split("/")[:-1])
        if folder:
            self.ensure_folder(folder)
        try:
            r = requests.put(self._dav_url(remote_path), data=content,
                             auth=(self.user, self.password),
                             timeout=max(TIMEOUT, 120), verify=self.verify)
        except requests.exceptions.RequestException as e:
            raise NextcloudUnreachable(_("Could not upload the file: {0}").format(e)) from e
        if r.status_code not in (200, 201, 204):
            raise NextcloudError(_("File upload failed ({0})").format(r.status_code))
        return remote_path

    def delete_path(self, remote_path):
        try:
            r = requests.delete(self._dav_url(remote_path),
                                auth=(self.user, self.password),
                                timeout=TIMEOUT, verify=self.verify)
        except requests.exceptions.RequestException as e:
            raise NextcloudUnreachable(str(e)) from e
        return r.status_code in (200, 204, 404)

    def download_file(self, remote_path) -> bytes:
        """GET the bytes back.

        A 404 is raised as DoesNotExistError rather than a generic failure:
        callers distinguish "the platform is unhappy" from "this file is gone",
        and only the second is worth surfacing to a user as a broken link.
        """
        try:
            r = requests.get(self._dav_url(remote_path),
                             auth=(self.user, self.password),
                             timeout=max(TIMEOUT, 120), verify=self.verify)
        except requests.exceptions.RequestException as e:
            raise NextcloudUnreachable(_("Could not download the file: {0}").format(e)) from e
        if r.status_code == 404:
            frappe.throw(_("The file no longer exists on the server: {0}").format(remote_path),
                         exc=frappe.DoesNotExistError)
        if r.status_code != 200:
            raise NextcloudError(_("File download failed ({0})").format(r.status_code))
        return r.content

    def path_exists(self, remote_path) -> bool:
        try:
            r = requests.request("PROPFIND", self._dav_url(remote_path),
                                 auth=(self.user, self.password),
                                 headers={"Depth": "0"},
                                 timeout=TIMEOUT, verify=self.verify)
        except requests.exceptions.RequestException as e:
            raise NextcloudUnreachable(str(e)) from e
        return r.status_code in (200, 207)

    def list_folder(self, remote_path):
        """One level of a folder: name, path, size, whether it is a folder.

        Depth 1 on purpose. Depth infinity on a large tree is slow and the
        picker only ever shows one level at a time.
        """
        try:
            r = requests.request("PROPFIND", self._dav_url(remote_path),
                                 auth=(self.user, self.password),
                                 headers={"Depth": "1"},
                                 timeout=TIMEOUT, verify=self.verify)
        except requests.exceptions.RequestException as e:
            raise NextcloudUnreachable(_("Could not reach the server: {0}").format(e)) from e
        if r.status_code == 404:
            return []
        if r.status_code not in (200, 207):
            raise NextcloudError(_("Could not list the folder ({0})").format(r.status_code))

        from urllib.parse import unquote
        ns = {"d": "DAV:"}
        root = ET.fromstring(r.text)
        base = f"/remote.php/dav/files/{self.user}"
        out = []
        for resp in root.findall("d:response", ns):
            href = unquote(resp.findtext("d:href", default="", namespaces=ns) or "")
            path = href[len(base):] if href.startswith(base) else href
            path = path.rstrip("/")
            if not path or path == remote_path.rstrip("/"):
                continue  # the folder itself
            props = resp.find("d:propstat/d:prop", ns)
            is_dir = props is not None and props.find("d:resourcetype/d:collection", ns) is not None
            size = props.findtext("d:getcontentlength", default="", namespaces=ns) if props is not None else ""
            out.append({
                "name": path.rsplit("/", 1)[-1],
                "path": path,
                "is_folder": bool(is_dir),
                "size": int(size) if size.isdigit() else None,
            })
        out.sort(key=lambda x: (not x["is_folder"], x["name"].lower()))
        return out

    def move_path(self, src, dest):
        """MOVE, creating the destination's parent first.

        Used to retire a file rather than destroy it. Overwrite is on: the
        destination is an archive location, and refusing to move because
        something is already there would strand the file at its live path.
        """
        folder = "/".join(dest.strip("/").split("/")[:-1])
        if folder:
            self.ensure_folder(folder)
        try:
            r = requests.request("MOVE", self._dav_url(src),
                                 auth=(self.user, self.password),
                                 headers={"Destination": self._dav_url(dest),
                                          "Overwrite": "T"},
                                 timeout=TIMEOUT, verify=self.verify)
        except requests.exceptions.RequestException as e:
            raise NextcloudUnreachable(str(e)) from e
        if r.status_code not in (201, 204):
            raise NextcloudError(_("Could not move the file ({0})").format(r.status_code))
        return dest
