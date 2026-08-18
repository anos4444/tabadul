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


def company_of(doctype, name):
    """The company on a document, when it has one.

    Many doctypes have no company at all — ToDo, Data Import, File. Those fall
    through to the general rule rather than being refused, because a per-company
    setup should not stop unrelated attachments from working.
    """
    if not (doctype and name):
        return None
    try:
        meta = frappe.get_meta(doctype)
    except Exception:
        return None
    if not meta.has_field("company"):
        return None
    return frappe.db.get_value(doctype, name, "company")


def rule_for(doctype, company=None):
    """The routing rule for a doctype, or None when it is not routed.

    A rule naming a company beats a general rule for the same doctype, so one
    ERP serving several companies can send each company's documents to its own
    Nextcloud while everything else follows a single default.
    """
    if not doctype:
        return None
    s = settings()
    if not s.get("storage_enabled"):
        return None

    candidates = [r for r in (s.get("storage_rules") or [])
                  if r.document_type == doctype and r.enabled]
    if not candidates:
        return None

    if company:
        for r in candidates:
            if r.get("company") == company:
                return r
    for r in candidates:
        if not r.get("company"):
            return r
    # Only company-specific rules exist and none matched: this document is not
    # routed. Falling back to another company's instance would be worse than
    # leaving it on local disk.
    return None


def instance_for(rule):
    """The Nextcloud a rule points at.

    Order: the rule's own instance, then the site default, then the connection
    configured directly on Settings. That last step is what keeps installs
    predating multi-tenant working untouched.
    """
    if rule is not None and rule.get("instance"):
        return frappe.get_cached_doc("Nextcloud Instance", rule.get("instance"))
    s = settings()
    if s.get("default_instance"):
        return frappe.get_cached_doc("Nextcloud Instance", s.get("default_instance"))
    return s


def instance_for_doc(doctype=None, name=None):
    """The Nextcloud a given document's attachments belong to.

    Used by the picker so browsing shows the tree the file would actually be
    filed in, rather than the default instance's tree while the document is
    routed elsewhere.
    """
    rule = rule_for(doctype, company_of(doctype, name)) if doctype else None
    return instance_for(rule)


def client_for(rule=None, instance_name=None):
    if instance_name:
        return NextcloudClient(frappe.get_cached_doc("Nextcloud Instance", instance_name))
    return NextcloudClient(instance_for(rule))


def route_for(file_doc):
    """The rule that governs this file, or None when it stays on local disk.

    Returns the rule rather than a boolean so the caller resolves it once:
    with company-aware matching, deciding "is this routed?" and "where to?"
    are the same lookup, and doing it twice invites the two answers to differ.
    """
    if getattr(file_doc, "is_folder", 0):
        return None
    # No attachment target means a site asset, not a document's file.
    if not file_doc.attached_to_doctype:
        return None
    rule = rule_for(file_doc.attached_to_doctype,
                    company_of(file_doc.attached_to_doctype, file_doc.attached_to_name))
    if not rule:
        return None
    if file_doc.is_private and not rule.include_private:
        return None
    if not file_doc.is_private and not rule.include_public:
        return None
    return rule


def should_route(file_doc) -> bool:
    return route_for(file_doc) is not None


def render_folder(file_doc, rule) -> str:
    """Build the remote folder from the rule's template.

    A template may reference fields of the attached document, e.g.
    ``Employee/{employee_number} - {employee_name}``. An unresolvable field
    renders as the literal segment rather than raising: a template that
    outlives a renamed field should file the document somewhere predictable,
    not refuse the upload.
    """
    target = instance_for(rule)
    template = (rule.path_template or target.get("default_path_template")
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

    root = (target.get("storage_root") or "Frappe").strip("/")
    return "/" + posixpath.join(root, *[x for x in segments if x])


def remote_path_for(file_doc, rule=None) -> str:
    if rule is None:
        rule = rule_for(file_doc.attached_to_doctype,
                        company_of(file_doc.attached_to_doctype, file_doc.attached_to_name))
    return posixpath.join(render_folder(file_doc, rule),
                          sanitize(file_doc.file_name))


def is_remote(file_doc) -> bool:
    return bool(file_doc.file_url and file_doc.file_url.startswith(PROXY_ROUTE))


def stored_ref(file_name):
    """Where a file lives: its path AND which server holds it."""
    return frappe.db.get_value("Nextcloud Stored File", {"file": file_name},
                               ["remote_path", "instance"], as_dict=True)


def stored_path(file_name):
    """The path we actually wrote, as recorded at upload time."""
    row = stored_ref(file_name)
    return row.remote_path if row else None


# ----------------------------------------------------------------- hooks


def _upload_with_retry(remote, content, attempts=3, client=None):
    """Retry only network failures, never rejections.

    A 403 means the credential is wrong and will be wrong again in two
    seconds. NextcloudUnreachable is a blip worth riding out, and riding it
    out is what keeps the private-file hard fail from tripping on noise.
    """
    client = client or NextcloudClient()
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
    rule = route_for(file_doc)
    if rule is None:
        return file_doc.save_file_on_filesystem()

    target = instance_for(rule)
    remote = remote_path_for(file_doc, rule)
    try:
        if target.doctype == "Nextcloud Instance" and not target.get("enabled"):
            # Disabling an instance is deliberate, but it must not become a
            # quiet route onto the ERP disk: a private file takes exactly the
            # same path as an unreachable server — refused, not filed locally.
            raise NextcloudError(
                _("Nextcloud instance {0} is disabled").format(target.name))
        _upload_with_retry(remote, file_doc._content, client=NextcloudClient(target))
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
    # Recorded so reads and deletes reach the same server later, even if the
    # rule is repointed at a different instance afterwards.
    file_doc.flags.tabadul_instance = (
        target.name if target.doctype == "Nextcloud Instance" else None)

    return {"file_name": posixpath.basename(remote), "file_url": file_doc.file_url}


def delete_file_data_content(file_doc, only_thumbnail=False):
    """hooks.delete_file_data_content — honour the configured retirement mode."""
    if not is_remote(file_doc):
        return file_doc.delete_file_from_filesystem(only_thumbnail=only_thumbnail)

    ref = stored_ref(file_doc.name)
    if not ref or not ref.remote_path:
        return
    remote = ref.remote_path

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

    # The instance recorded at upload time, not the one the rule points at
    # now: a rule repointed at a new server must not send us hunting for this
    # file somewhere it was never written.
    target = (frappe.get_cached_doc("Nextcloud Instance", ref.instance)
              if ref.get("instance") else settings())
    behaviour = target.get("delete_behaviour") or "Archive"
    if behaviour == "Keep":
        return

    # Retiring the stored object must never prevent the user from deleting
    # their own record. If Nextcloud is unreachable, or the object was removed
    # on the platform directly, the File row still has to go — otherwise the
    # attachment becomes permanently undeletable and the person is stuck with
    # no way out from inside ERPNext. The failure is recorded instead, because
    # an object left behind is a cleanup task, not a reason to block work.
    try:
        client = NextcloudClient(target)
        if behaviour == "Delete":
            client.delete_path(remote)
            return

        root = (target.get("storage_root") or "Frappe").strip("/")
        archive = (target.get("archive_folder") or "_deleted").strip("/")
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
