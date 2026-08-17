"""Hourly upkeep.

Two jobs, deliberately separate: closing expired packages is local bookkeeping
and must work even when Nextcloud is down, while reconciliation needs the
server and is allowed to fail quietly until the next hour.
"""
import frappe
from frappe import _
from frappe.utils import getdate, now_datetime, nowdate

from tabadul.nextcloud_client import NextcloudClient, NextcloudError, NextcloudUnreachable

ACTIVE, EXPIRED = "Active", "Expired"


def close_expired_packages():
    """Flip packages whose date has passed, and tell the creator once.

    Nextcloud enforces expiry itself — this does not control access, it keeps
    our record honest so the list does not show closed packages as active.
    """
    due = frappe.get_all("Share Package",
                         filters={"status": ACTIVE, "expires_on": ["<", nowdate()]},
                         fields=["name", "title", "created_by_user", "expires_on"])
    for pkg in due:
        frappe.db.set_value("Share Package", pkg.name, "status", EXPIRED,
                            update_modified=False)
        _notify_closed(pkg)
    if due:
        frappe.db.commit()
    return len(due)


def _notify_closed(pkg):
    if not pkg.get("created_by_user"):
        return
    try:
        frappe.sendmail(
            recipients=[pkg["created_by_user"]],
            subject=_("Share package expired: {0}").format(pkg["title"]),
            message=_("<p>The package <b>{0}</b> expired on {1}, and recipients can no "
                      "longer reach it.</p>").format(
                          frappe.utils.escape_html(pkg["title"]), pkg["expires_on"]),
            reference_doctype="Share Package", reference_name=pkg["name"],
        )
    except Exception:
        # No mail transport yet on some installs. A missing courtesy email must
        # never stop a package from being closed.
        frappe.log_error(f"tabadul: could not email closing summary for {pkg['name']}",
                         "tabadul.close_expired_packages")


def reconcile_with_nextcloud():
    """Detect shares deleted directly in Nextcloud, and refresh download counts.

    Nextcloud is the source of truth for whether a share exists. Someone may
    revoke one from its own UI, and this record would otherwise keep claiming
    the recipient still has access.
    """
    packages = frappe.get_all("Share Package", filters={"status": ACTIVE}, pluck="name")
    if not packages:
        return 0

    try:
        client = NextcloudClient()
    except Exception:
        return 0                                  # unconfigured: nothing to do

    touched = 0
    for name in packages:
        doc = frappe.get_doc("Share Package", name)
        changed = False
        for r in doc.recipients:
            if not r.nc_share_id or r.status == "Cancelled":
                continue
            try:
                remote = client.get_share(r.nc_share_id)
            except NextcloudUnreachable:
                return touched                    # server down: stop, retry next hour
            except NextcloudError:
                continue
            if remote is None:
                r.status = "Cancelled"
                r.error_message = _("The share was deleted directly in Nextcloud")
                changed = True
                continue
            count = client.get_download_count(r.nc_share_token)
            if count is not None and count != (r.download_count or 0):
                r.download_count = count
                changed = True
        if changed:
            doc.save(ignore_permissions=True)
            touched += 1
    if touched:
        frappe.db.commit()
    return touched
