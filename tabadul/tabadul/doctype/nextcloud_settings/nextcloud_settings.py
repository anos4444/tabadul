from frappe import _
import frappe
from frappe.model.document import Document


class NextcloudSettings(Document):
    def validate(self):
        # a trailing slash turns every OCS path into a double slash
        if self.base_url:
            self.base_url = self.base_url.strip().rstrip("/")
        if self.default_expiry_days is not None and self.default_expiry_days < 0:
            frappe.throw(_("Expiry cannot be negative"))

    @frappe.whitelist()
    def test_connection(self):
        """Prove the service account works before anyone relies on it."""
        from tabadul.nextcloud_client import NextcloudClient
        try:
            info = NextcloudClient().whoami()
            msg = _("Connected — {0}").format(info)
        except Exception as e:
            msg = _("Connection failed — {0}").format(e)
        self.db_set("connection_status", msg, update_modified=False)
        return msg
