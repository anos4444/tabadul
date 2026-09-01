import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, getdate, now_datetime, nowdate

from tabadul.nextcloud_client import (
    SHARE_TYPE_EMAIL, SHARE_TYPE_LINK, NextcloudClient, NextcloudError,
    NextcloudUnreachable, generate_password,
)

ACTIVE, EXPIRED, CANCELLED, DRAFT = "Active", "Expired", "Cancelled", "Draft"
R_ACTIVE, R_MANUAL, R_CANCELLED, R_FAILED, R_NEW = (
    "Active", "Awaiting manual delivery", "Cancelled", "Failed", "Not created")


class SharePackage(Document):
    def validate(self):
        if not self.folder_path and not self.files:
            frappe.throw(_("Specify at least one folder or file"))
        seen = set()
        for r in self.recipients:
            email = (r.email or "").strip().lower()
            if email in seen:
                frappe.throw(_("Email {0} appears more than once in the recipient list").format(email))
            seen.add(email)
        if not self.expires_on:
            days = frappe.get_single("Nextcloud Settings").default_expiry_days or 14
            self.expires_on = add_days(nowdate(), days)
        # Only guard the date when it is actually being set. Enforcing it on
        # every save would make an expired package impossible to cancel or
        # correct - exactly when you most need to touch it.
        if (self.is_new() or self.has_value_changed("expires_on")) \
                and getdate(self.expires_on) < getdate(nowdate()):
            frappe.throw(_("The expiry date is in the past"))
        if not self.created_by_user:
            self.created_by_user = frappe.session.user
            self.created_on = now_datetime()

    # ------------------------------------------------------------- creating
    @frappe.whitelist()
    def create_shares(self):
        """One Nextcloud share per recipient.

        Per-recipient rather than one link for everyone, so a single person's
        access can be revoked without disturbing the rest, and a forwarded link
        is traceable to whoever it was issued to.

        Email shares are preferred. Where the instance cannot send mail the
        share is still created as a link and marked for manual delivery, so
        per-recipient separation survives even without SMTP. The moment mail
        works, the same code takes the email path with no change.
        """
        if self.status == CANCELLED:
            frappe.throw(_("This package is cancelled"))
        if not self.recipients:
            frappe.throw(_("There are no recipients"))

        client = NextcloudClient()
        paths = self._paths()
        created, manual, failed = 0, 0, 0
        passwords = {}

        for r in self.recipients:
            if r.nc_share_id:
                continue                        # idempotent: never double-issue
            password = generate_password()
            try:
                share = self._create_one(client, r, paths[0], password)
            except NextcloudUnreachable as e:
                r.status, r.error_message = R_FAILED, str(e)
                failed += 1
                continue
            except NextcloudError as e:
                r.status, r.error_message = R_FAILED, str(e)
                failed += 1
                continue

            r.nc_share_id = share["id"]
            r.nc_share_token = share.get("token")
            r.share_url = share.get("url")
            r.error_message = None
            if share["share_type"] == SHARE_TYPE_EMAIL:
                r.status = R_ACTIVE
                created += 1
            else:
                r.status = R_MANUAL
                manual += 1
            passwords[r.email] = password

            if self.download_limit:
                if not client.set_download_limit(r.nc_share_token, self.download_limit):
                    r.error_message = _("Could not apply the download limit to this share")

        if created or manual:
            self.status = ACTIVE
        self.save(ignore_permissions=True)
        # The shares now exist on Nextcloud. Rolling back here would discard
        # the share IDs while the shares themselves stay live — orphaned
        # access nobody can revoke from this app.
        frappe.db.commit()  # nosemgrep

        # Passwords are returned once, to be read and delivered by hand. They
        # are never stored on the document and never emailed with the link.
        return {
            "created": created, "manual": manual, "failed": failed,
            "passwords": passwords,
            "message": self._summary(created, manual, failed),
        }

    def _create_one(self, client, recipient, path, password):
        expire = str(self.expires_on) if self.expires_on else None
        note = self.purpose or self.title
        try:
            return client.create_share(
                path=path, password=password, expire_date=expire,
                share_type=SHARE_TYPE_EMAIL, share_with=recipient.email,
                allow_download=bool(self.allow_download), note=note)
        except NextcloudError as e:
            # No mail transport is a property of the instance, not of this
            # request; degrade to a link the operator delivers by hand rather
            # than failing the package.
            # Matches Nextcloud's OWN error text, not ours, so it is not a
            # translatable string: the instance may answer in either language
            # depending on its configured locale, and both must be recognised.
            if "mail" in str(e).lower() or "بريد" in str(e):
                return client.create_share(
                    path=path, password=password, expire_date=expire,
                    share_type=SHARE_TYPE_LINK,
                    allow_download=bool(self.allow_download), note=note)
            raise

    def _paths(self):
        if self.folder_path:
            return [self.folder_path]
        return [f.remote_path for f in self.files if f.remote_path]

    @staticmethod
    def _summary(created, manual, failed):
        bits = []
        if created:
            bits.append(_("{0} emailed").format(created))
        if manual:
            bits.append(_("{0} awaiting manual delivery").format(manual))
        if failed:
            bits.append(_("{0} failed").format(failed))
        return ", ".join(bits) or _("Nothing was created")

    # ------------------------------------------------------------ revoking
    @frappe.whitelist()
    def cancel_shares(self, reason=None):
        """Delete every share now. Records who, when and why."""
        client = NextcloudClient()
        removed, stuck = 0, []
        for r in self.recipients:
            if not r.nc_share_id:
                continue
            try:
                client.delete_share(r.nc_share_id)
                r.status = R_CANCELLED
                removed += 1
            except NextcloudError as e:
                stuck.append(f"{r.email}: {e}")
                r.error_message = str(e)

        self.status = CANCELLED
        self.cancelled_by = frappe.session.user
        self.cancelled_on = now_datetime()
        self.cancel_reason = reason
        self.save(ignore_permissions=True)
        # Revocation already happened on Nextcloud. A rollback would show the
        # package as active when its shares are gone, which is the more
        # dangerous of the two ways to be wrong.
        frappe.db.commit()  # nosemgrep

        if stuck:
            # Partial revocation is a security matter — say so loudly rather
            # than reporting a clean cancellation.
            frappe.throw(_("Partially cancelled. Not deleted: {0}").format("; ".join(stuck)))
        return {"removed": removed}
