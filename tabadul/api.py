"""Whitelisted entry points. The app-password never crosses this boundary."""
import frappe
from frappe import _

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
            _("The platform records share creation, share deletion, service-account "
              "sign-in, and download counts. It provides no record of who downloaded a "
              "file or when, and no reliable tracking of share-page views.")
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


@frappe.whitelist()
def download_attachment(file):
    """Serve a Nextcloud-stored attachment through Frappe's permission layer.

    This is the security boundary for stored attachments. Frappe's own
    /private/files/ route refuses to serve bytes the session user may not see;
    once the bytes live on Nextcloud that check no longer runs, so it is
    reimplemented here. The authority is the ATTACHED DOCUMENT, not the File
    row — a File is readable by more people than the document it hangs off,
    and using the File's own permission would quietly widen access.
    """
    from tabadul.attachments import is_remote, stored_path

    if frappe.session.user == "Guest":
        raise frappe.PermissionError(_("Login required"))

    doc = frappe.get_doc("File", file)

    if not is_remote(doc):
        frappe.throw(_("This file is not stored on Nextcloud"))

    if doc.attached_to_doctype and doc.attached_to_name:
        if not frappe.has_permission(doc.attached_to_doctype, ptype="read",
                                     doc=doc.attached_to_name,
                                     user=frappe.session.user):
            raise frappe.PermissionError(
                _("Not permitted to read {0} {1}").format(doc.attached_to_doctype, doc.attached_to_name))
    elif doc.is_private and frappe.session.user != doc.owner:
        # Unattached private file: owner only, unless File itself is readable.
        if not frappe.has_permission("File", "read", doc=doc.name):
            raise frappe.PermissionError(_("Not permitted to read this file"))

    remote = stored_path(doc.name)
    if not remote:
        frappe.throw(_("No remote path is recorded for this file: {0}").format(file),
                     exc=frappe.DoesNotExistError)

    content = NextcloudClient().download_file(remote)

    frappe.local.response.filename = doc.file_name
    frappe.local.response.filecontent = content
    frappe.local.response.type = "download"
    frappe.local.response.display_content_as = "attachment"


@frappe.whitelist()
def storage_stats():
    """Counters only — how many files are stored and how much they occupy.

    No per-user or per-download figures: the platform does not report them for
    stored files any more than it does for shares, and inventing them here
    would contradict what audit_view is careful to say.
    """
    frappe.only_for("System Manager")
    rows = frappe.db.get_all(
        "Nextcloud Stored File",
        fields=["attached_to_doctype as doctype", "count(name) as files",
                "sum(file_size) as bytes"],
        group_by="attached_to_doctype", order_by="files desc")
    return {"total_files": frappe.db.count("Nextcloud Stored File"),
            "by_doctype": rows}


@frappe.whitelist()
def browse(path=None):
    """List one level of the Nextcloud tree, for the attach picker.

    Write permission on the target is not checked here because this only
    reveals the service account's own tree, which every Desk user with the
    picker can already reach through the same account. Attaching is where the
    permission check belongs, and attach_remote does it.
    """
    from tabadul.attachments import settings

    if frappe.session.user == "Guest":
        raise frappe.PermissionError(_("Login required"))

    s = settings()
    if not s.get("enable_upload_picker"):
        raise frappe.PermissionError(
            _("Attaching existing Nextcloud files is disabled"))

    root = "/" + (s.get("storage_root") or "Frappe").strip("/")
    target = path or root

    # Keep the picker inside the configured root: the service account may hold
    # unrelated material and the picker is not an excuse to browse it.
    if not (target == root or target.startswith(root + "/")):
        frappe.throw(_("Path is outside the configured root folder"))

    return {"path": target, "root": root, "entries": NextcloudClient().list_folder(target)}


@frappe.whitelist()
def attach_remote(doctype, docname, remote_path, is_private=1):
    """Attach a file that already exists on Nextcloud, without re-uploading.

    The bytes stay where they are; only a File row and its mapping are made.
    Permission is checked against the document being attached to, matching the
    rule used when serving the file back.
    """
    from tabadul.attachments import PENDING, PROXY_ROUTE, settings

    if frappe.session.user == "Guest":
        raise frappe.PermissionError(_("Login required"))

    if not frappe.has_permission(doctype, ptype="write", doc=docname,
                                 user=frappe.session.user):
        raise frappe.PermissionError(
            _("Not permitted to attach files to {0} {1}").format(doctype, docname))

    s = settings()
    if not s.get("enable_upload_picker"):
        # Hiding the button is presentation, not a control. Both endpoints
        # refuse independently so a crafted request cannot browse the tree.
        raise frappe.PermissionError(
            _("Attaching existing Nextcloud files is disabled"))

    root = "/" + (s.get("storage_root") or "Frappe").strip("/")
    if not (remote_path == root or remote_path.startswith(root + "/")):
        frappe.throw(_("Path is outside the configured root folder"))

    client = NextcloudClient()
    if not client.path_exists(remote_path):
        frappe.throw(_("That file no longer exists on Nextcloud: {0}").format(remote_path),
                     exc=frappe.DoesNotExistError)

    doc = frappe.get_doc({
        "doctype": "File",
        "file_name": remote_path.rstrip("/").rsplit("/", 1)[-1],
        "attached_to_doctype": doctype,
        "attached_to_name": docname,
        "is_private": int(is_private or 0),
        # after_insert rewrites this once the name exists, and creates the
        # mapping from the flag below.
        "file_url": f"{PROXY_ROUTE}?file={PENDING}",
    })
    doc.flags.tabadul_remote_path = remote_path
    doc.insert(ignore_permissions=True)

    return {"file": doc.name, "file_url": doc.file_url, "remote_path": remote_path}


@frappe.whitelist()
def get_picker_settings():
    """Whether the upload dialog should offer a Nextcloud source.

    Deliberately tiny and free of credentials: it runs on every Desk page load
    for every user.
    """
    if frappe.session.user == "Guest":
        return {"enabled": False}
    try:
        s = frappe.get_cached_doc("Nextcloud Settings")
    except Exception:
        return {"enabled": False}
    return {"enabled": bool(s.get("storage_enabled") and s.get("enable_upload_picker"))}
