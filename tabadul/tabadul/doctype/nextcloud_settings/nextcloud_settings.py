import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class NextcloudSettings(Document):
    def validate(self):
        # a trailing slash turns every OCS path into a double slash
        if self.base_url:
            self.base_url = self.base_url.strip().rstrip("/")
        if self.default_expiry_days is not None and self.default_expiry_days < 0:
            frappe.throw(_("Expiry cannot be negative"))
        self._verify_connection_when_it_matters()

    # ------------------------------------------------------------------ #

    def _verify_connection_when_it_matters(self):
        """Check the credentials on save, and record the result.

        Silence after saving credentials is the wrong feedback: a wrong
        password would otherwise surface at the first upload, to whoever
        happened to be attaching a file, rather than to the person who typed
        it. Only runs when something connection-relevant changed, so ordinary
        edits to expiry or templates do not pay for a network round trip.
        """
        watched = ("base_url", "service_user", "app_password", "verify_tls")
        credentials_changed = self.is_new() or any(
            self.has_value_changed(f) for f in watched
        )
        turning_storage_on = (
            self.storage_enabled and self.has_value_changed("storage_enabled")
        )

        if not (credentials_changed or turning_storage_on):
            return
        if not (self.base_url and self.service_user):
            self.connection_status = _("Not configured")
            return

        from tabadul.nextcloud_client import NextcloudClient

        stamp = now_datetime().strftime("%Y-%m-%d %H:%M")
        try:
            # Pass self: the database still holds the previous password during
            # save, so a client built from it would test the wrong credentials.
            who = NextcloudClient(self).whoami()
            self.connection_status = _("Connected as {0} · checked {1}").format(who, stamp)
        except Exception as e:
            self.connection_status = _("Not connected — {0} · checked {1}").format(str(e), stamp)
            if self.storage_enabled:
                # Enabling storage against a backend that cannot be reached is
                # never what someone means. Every private upload would fail and
                # every public one would quietly land on local disk.
                frappe.throw(
                    _("Attachment storage was not enabled: the Nextcloud "
                      "connection failed.<br><br>{0}").format(
                          frappe.utils.escape_html(str(e))),
                    title=_("Cannot enable storage"),
                )

    # ------------------------------------------------------------------ #

    @frappe.whitelist()
    def test_connection(self):
        """Prove the service account works before anyone relies on it."""
        from tabadul.nextcloud_client import NextcloudClient

        stamp = now_datetime().strftime("%Y-%m-%d %H:%M")
        try:
            info = NextcloudClient(self).whoami()
            msg = _("Connected as {0} · checked {1}").format(info, stamp)
            ok = True
        except Exception as e:
            msg = _("Not connected — {0} · checked {1}").format(str(e), stamp)
            ok = False
        self.db_set("connection_status", msg, update_modified=False)
        return {"ok": ok, "message": msg}
