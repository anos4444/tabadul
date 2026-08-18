import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class NextcloudInstance(Document):
    """One Nextcloud server and the account tabadul uses on it.

    Field names deliberately match Nextcloud Settings. NextcloudClient accepts
    either object, so nothing in the transport layer needs to know whether it
    is talking to a named instance or to the single legacy configuration.
    """

    def validate(self):
        if self.base_url:
            self.base_url = self.base_url.strip().rstrip("/")
        self._verify_connection_when_it_matters()

    def _verify_connection_when_it_matters(self):
        watched = ("base_url", "service_user", "app_password", "verify_tls")
        if not (self.is_new() or any(self.has_value_changed(f) for f in watched)):
            return
        if not (self.base_url and self.service_user):
            self.connection_status = _("Not configured")
            return

        from tabadul.nextcloud_client import NextcloudClient

        stamp = now_datetime().strftime("%Y-%m-%d %H:%M")
        try:
            who = NextcloudClient(self).whoami()
            self.connection_status = _("Connected as {0} · checked {1}").format(who, stamp)
        except Exception as e:
            self.connection_status = _("Not connected — {0} · checked {1}").format(e, stamp)
            if self.enabled:
                frappe.throw(
                    _("This instance was not enabled: the connection failed.<br><br>{0}")
                    .format(frappe.utils.escape_html(str(e))),
                    title=_("Cannot enable instance"),
                )

    def on_trash(self):
        """Refuse to delete an instance that files still live on.

        Removing it would strand every mapping pointing at it: the files would
        still exist on that server with nothing in Frappe able to reach them.
        """
        stored = frappe.db.count("Nextcloud Stored File", {"instance": self.name})
        if stored:
            frappe.throw(
                _("{0} stored file(s) still live on this instance. Move or remove them first.")
                .format(stored),
                title=_("Instance is in use"),
            )

        # A rule left pointing at a deleted instance would fail at the next
        # upload, to whoever happened to be attaching a file rather than to the
        # person who deleted it.
        rules = frappe.db.count("Nextcloud Storage Rule", {"instance": self.name})
        if rules:
            frappe.throw(
                _("{0} storage rule(s) still send attachments to this instance. "
                  "Repoint them first.").format(rules),
                title=_("Instance is in use"),
            )

        if frappe.db.get_single_value("Nextcloud Settings", "default_instance") == self.name:
            frappe.throw(
                _("This is the default instance for the site. Choose another default first."),
                title=_("Instance is in use"),
            )

    @frappe.whitelist()
    def test_connection(self):
        from tabadul.nextcloud_client import NextcloudClient

        stamp = now_datetime().strftime("%Y-%m-%d %H:%M")
        try:
            info = NextcloudClient(self).whoami()
            msg = _("Connected as {0} · checked {1}").format(info, stamp)
            ok = True
        except Exception as e:
            msg = _("Not connected — {0} · checked {1}").format(e, stamp)
            ok = False
        self.db_set("connection_status", msg, update_modified=False)
        return {"ok": ok, "message": msg}
