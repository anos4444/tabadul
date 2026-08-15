"""Whitelisted entry points. The app-password never crosses this boundary."""
import frappe

from tabadul.nextcloud_client import NextcloudClient, NextcloudError


@frappe.whitelist()
def audit_view(package):
    """What the platform actually recorded about this package.

    Deliberately narrow. Nextcloud reports share creation, share deletion,
    authentication, and download COUNTS. It does not provide a per-recipient
    download log, and anonymous views of a share page are not reliably
    recorded. This returns only the first set, and states the limits, so no
    reader infers a per-person trail that does not exist.
    """
    doc = frappe.get_doc("Share Package", package)
    doc.check_permission("read")

    rows = []
    for r in doc.recipients:
        rows.append({
            "recipient": r.full_name,
            "email": r.email,
            "organisation": r.organisation,
            "status": r.status,
            "share_id": r.nc_share_id,
            "downloads": r.download_count,       # a count, never a who
            "password_sent_via": r.password_sent_via,
            "error": r.error_message,
        })

    versions = frappe.get_all(
        "Version", filters={"ref_doctype": "Share Package", "docname": package},
        fields=["owner", "creation"], order_by="creation desc", limit=20)

    return {
        "package": {
            "title": doc.title, "status": doc.status, "expires_on": doc.expires_on,
            "created_by": doc.created_by_user, "created_on": doc.created_on,
            "cancelled_by": doc.cancelled_by, "cancelled_on": doc.cancelled_on,
            "cancel_reason": doc.cancel_reason,
        },
        "recipients": rows,
        "changes": versions,
        # Rendered verbatim in the UI. Do not soften or remove it.
        "coverage_note": (
            "تسجّل المنصة إنشاء المشاركة وإلغاءها والدخول إلى حساب الخدمة، "
            "وعدد مرات التنزيل. لا توفّر المنصة سجلًا يبيّن مَن نزّل الملف "
            "أو متى، ولا تتبّعًا موثوقًا لفتح صفحة المشاركة."
        ),
    }


@frappe.whitelist()
def check_connection():
    try:
        return {"ok": True, "who": NextcloudClient().whoami()}
    except NextcloudError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
