"""Route Frappe attachments to Nextcloud instead of local disk.

This is the second thing tabadul does. The first — Share Package — hands files
*out* to named external recipients. This one decides where a document's
attachments *live* in the first place.

Frappe supports this properly: when an app defines a ``write_file`` hook,
``File.save_file()`` calls it instead of ``save_file_on_filesystem()``. The
delete path is hookable the same way. Reads are not, which is why
``overrides/file.py`` exists alongside this module.

Opt-in per doctype, deliberately. A site-wide switch would also capture print
format assets, avatars and every embedded image, and the failure mode of a
storage backend that is briefly unreachable is far worse when it sits under
everything than when it sits under the doctypes you chose.
"""

import posixpath
import re
import time
import unicodedata

import frappe
from frappe import _

from tabadul.nextcloud_client import (
    NextcloudClient,
    NextcloudError,
    NextcloudUnreachable,
)

# Kept in File.file_url so our own files are recognisable without a join or an
# extra column on every File row in the site.
PROXY_ROUTE = "/api/method/tabadul.api.download_attachment"

# Placeholder used when a File has no name yet; after_insert() replaces it.
PENDING = "pending"

_ILLEGAL = re.compile(r'[\\/:*?"<>|]')
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def sanitize(value, max_length: int = 180) -> str:
    """Make one path segment safe, preserving Arabic.

    Normalising to NFC is not cosmetic. Nextcloud composes on write — an alef
    with hamza arrives as U+0627 U+0654 and is stored as U+0623 — so a path
    built here and a path read back from the platform will not compare equal
    unless both sides are normalised. That mismatch reads as a missing file.
    """
    value = "" if value is None else str(value)
    value = _ILLEGAL.sub("-", value)
    value = _CONTROL.sub("", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value[:max_length]
    return unicodedata.normalize("NFC", value) or "unnamed"


def settings():
    return frappe.get_cached_doc("Nextcloud Settings")


def rule_for(doctype):
    """The routing rule for a doctype, or None when it is not routed."""
    if not doctype:
        return None
    s = settings()
    if not s.get("storage_enabled"):
        return None
    for rule in s.get("storage_rules") or []:
        if rule.document_type == doctype and rule.enabled:
            return rule
    return None


def should_route(file_doc) -> bool:
    if getattr(file_doc, "is_folder", 0):
        return False
    # No attachment target means a site asset, not a document's file.
    if not file_doc.attached_to_doctype:
        return False
    rule = rule_for(file_doc.attached_to_doctype)
    if not rule:
        return False
    if file_doc.is_private and not rule.include_private:
        return False
    if not file_doc.is_private and not rule.include_public:
        return False
    return True


def render_folder(file_doc, rule) -> str:
    """Build the remote folder from the rule's template.

    A template may reference fields of the attached document, e.g.
    ``Employee/{employee_number} - {employee_name}``. An unresolvable field
    renders as the literal segment rather than raising: a template that
    outlives a renamed field should file the document somewhere predictable,
    not refuse the upload.
    """
    s = settings()
    template = (rule.path_template or s.get("default_path_template")
                or "{doctype}/{name}").strip("/")

    context = {"doctype": file_doc.attached_to_doctype,
               "name": file_doc.attached_to_name}
    if "{" in template:
        try:
            doc = frappe.get_cached_doc(file_doc.attached_to_doctype,
                                        file_doc.attached_to_name)
            for k, v in doc.as_dict().items():
                if isinstance(v, (str, int, float)) and v is not None:
                    context[k] = v
        except Exception:
            # Not readable in this context; doctype/name is still a valid home.
            pass

    segments = []
    for seg in template.split("/"):
        try:
            segments.append(sanitize(seg.format(**context)))
        except (KeyError, IndexError, ValueError):
            segments.append(sanitize(seg))

    root = (s.get("storage_root") or "Frappe").strip("/")
    return "/" + posixpath.join(root, *[x for x in segments if x])


def remote_path_for(file_doc) -> str:
    rule = rule_for(file_doc.attached_to_doctype)
    return posixpath.join(render_folder(file_doc, rule),
                          sanitize(file_doc.file_name))


def is_remote(file_doc) -> bool:
    return bool(file_doc.file_url and file_doc.file_url.startswith(PROXY_ROUTE))


def stored_path(file_name):
    """The path we actually wrote, as recorded at upload time."""
    return frappe.db.get_value("Nextcloud Stored File",
                               {"file": file_name}, "remote_path")


# ----------------------------------------------------------------- hooks


def _upload_with_retry(remote, content, attempts=3):
    """Retry only network failures, never rejections.

    A 403 means the credential is wrong and will be wrong again in two
    seconds. NextcloudUnreachable is a blip worth riding out, and riding it
    out is what keeps the private-file hard fail from tripping on noise.
    """
    client = NextcloudClient()
    last = None
    for attempt in range(attempts):
        try:
            return client.upload_file(remote, content)
        except NextcloudUnreachable as e:
            last = e
            if attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last


def write_file(file_doc):
    """hooks.write_file — replaces local storage for routed doctypes.

    Private files never fall back to local disk. That is the whole point: an
    employee's ID scan that quietly lands on the ERP server because Nextcloud
    blinked is exactly the outcome this app exists to prevent, and it would be
    invisible until someone audited the disk. Public files may fall back —
    a product image on local disk harms nobody, and blocking the upload would.
    """
    if not should_route(file_doc):
        return file_doc.save_file_on_filesystem()

    remote = remote_path_for(file_doc)
    try:
        _upload_with_retry(remote, file_doc._content)
    except (NextcloudError, NextcloudUnreachable) as e:
        if file_doc.is_private:
            frappe.log_error(
                title="tabadul: private upload refused",
                message=f"{file_doc.file_name} -> {remote}\n{e}",
            )
            frappe.throw(
                _("This file is private and could not be stored on Nextcloud, "
                  "so it has not been saved. It was not written to the server "
                  "disk. Please retry once the connection is restored.<br><br>{0}"
                  ).format(frappe.utils.escape_html(str(e))),
                title=_("Private file not stored"),
            )
        # Public: degrade rather than block. migrate_attachments.plan() lists
        # these later as candidates, since they look exactly like pre-rule files.
        frappe.logger("tabadul").warning(
            f"public attachment fell back to local disk: {file_doc.file_name} ({e})")
        return file_doc.save_file_on_filesystem()

    # Document.insert() assigns the name before running validate(), so the name
    # is normally present here. It is not guaranteed for every code path that
    # reaches save_file(), though, and a URL silently missing its file argument
    # would fail only later, at download time. Emit a marker instead and let
    # after_insert() rewrite it once the name definitely exists.
    file_doc.file_url = (
        f"{PROXY_ROUTE}?file={frappe.utils.quoted(file_doc.name)}"
        if file_doc.name else f"{PROXY_ROUTE}?file={PENDING}"
    )
    file_doc.flags.tabadul_remote_path = remote

    return {"file_name": posixpath.basename(remote), "file_url": file_doc.file_url}


def delete_file_data_content(file_doc, only_thumbnail=False):
    """hooks.delete_file_data_content — honour the configured retirement mode."""
    if not is_remote(file_doc):
        return file_doc.delete_file_from_filesystem(only_thumbnail=only_thumbnail)

    remote = stored_path(file_doc.name)
    if not remote:
        return

    # Frappe's content-hash deduplication means several File rows can share one
    # stored object. Retiring it while another row still points at it would
    # break that attachment, on a different document, with no warning. Core
    # guards its on-disk files the same way.
    others = frappe.db.count("Nextcloud Stored File",
                             {"remote_path": remote, "file": ["!=", file_doc.name]})
    if others:
        frappe.logger("tabadul").info(
            f"not retiring {remote}: {others} other attachment(s) still reference it")
        return

    s = settings()
    behaviour = s.get("delete_behaviour") or "Archive"
    if behaviour == "Keep":
        return

    # Retiring the stored object must never prevent the user from deleting
    # their own record. If Nextcloud is unreachable, or the object was removed
    # on the platform directly, the File row still has to go — otherwise the
    # attachment becomes permanently undeletable and the person is stuck with
    # no way out from inside ERPNext. The failure is recorded instead, because
    # an object left behind is a cleanup task, not a reason to block work.
    try:
        client = NextcloudClient()
        if behaviour == "Delete":
            client.delete_path(remote)
            return

        root = (s.get("storage_root") or "Frappe").strip("/")
        archive = (s.get("archive_folder") or "_deleted").strip("/")
        rel = remote.lstrip("/")
        if rel.startswith(root + "/"):
            rel = rel[len(root) + 1:]
        moved = client.move_path(remote, f"/{root}/{archive}/{rel}")
        if moved is None:
            frappe.logger("tabadul").info(
                f"nothing to retire at {remote}; it was already gone")
    except Exception as e:
        frappe.log_error(
            title="tabadul: could not retire stored file",
            message=(f"file: {file_doc.name}\nremote: {remote}\n"
                     f"behaviour: {behaviour}\n\n{e}\n\n"
                     "The attachment was deleted in Frappe regardless. The "
                     "object may still exist on Nextcloud and can be removed "
                     "there if it is no longer wanted."),
        )


def before_write_file(file_size=None, **kwargs):
    """hooks.before_write_file — reserved for quota checks. Kept cheap."""
    return
