"""File doctype override — the read path.

Frappe hooks writing and deleting a file but not reading one:
``File.get_content()`` opens ``get_full_path()`` off local disk directly. Every
server-side consumer goes through it — print formats, email attachments, image
resizing, ``frappe.attach_print`` — so without this override a stored
attachment would download fine in a browser and be invisible to all of them.

Kept as thin as possible. Anything that is not ours falls straight through to
core, so a site running this app behaves identically for every file that was
never routed to Nextcloud.
"""

import frappe
from frappe.core.doctype.file.file import File

from tabadul.attachments import PROXY_ROUTE, is_remote, stored_path
from tabadul.nextcloud_client import NextcloudClient

# Mirrors frappe.core.doctype.file.file.FILE_ENCODING_OPTIONS
_ENCODINGS = ("utf-8", "windows-1250", "windows-1252")


class NextcloudFile(File):
    def get_content(self, encodings=None):
        remote = stored_path(self.name) if is_remote(self) else None
        if not remote:
            return super().get_content(encodings=encodings)

        # A freshly created doc still carries its bytes in memory; core's path
        # handles that correctly and cheaply.
        if self.get("content"):
            return super().get_content(encodings=encodings)

        if encodings is None:
            encodings = _ENCODINGS

        self._content = NextcloudClient().download_file(remote)
        for encoding in encodings:
            try:
                self._content = self._content.decode(encoding)
                break
            except (UnicodeDecodeError, AttributeError):
                continue
        return self._content

    def exists_on_disk(self):
        if not is_remote(self):
            return super().exists_on_disk()
        remote = stored_path(self.name)
        return bool(remote) and NextcloudClient().path_exists(remote)

    def get_full_path(self):
        # Core would treat our proxy URL as a filesystem path and throw.
        if is_remote(self):
            return self.file_url
        return super().get_full_path()

    def validate_file_url(self):
        if is_remote(self):
            return
        return super().validate_file_url()

    def validate_file_path(self):
        # Core resolves file_url against the site's files directory and rejects
        # anything outside it. Our URL is an API route, not a path.
        if is_remote(self):
            return
        return super().validate_file_path()



def _adopt_deduplicated(doc):
    """Return the remote path this File was deduplicated onto, or None."""
    if not is_remote(doc):
        return None
    if frappe.db.exists("Nextcloud Stored File", {"file": doc.name}):
        return None

    source = (doc.file_url or "").split("?file=")[-1]
    if not source or source == doc.name:
        return None

    remote = stored_path(source)
    if not remote:
        return None

    doc.db_set("file_url", f"{PROXY_ROUTE}?file={frappe.utils.quoted(doc.name)}",
               update_modified=False)
    doc.file_url = f"{PROXY_ROUTE}?file={frappe.utils.quoted(doc.name)}"
    return remote


def after_insert(doc, method=None):
    """Record where the bytes actually went.

    The path cannot be recomputed later. It is rendered from the attached
    document's fields at upload time, and those fields move — an employee is
    renamed and the folder name goes with them. Recomputing would then look in
    a folder that no longer exists. Storing it is what keeps read and delete
    correct across such a change.
    """
    remote = doc.flags.get("tabadul_remote_path")

    if not remote:
        # Frappe deduplicates by content hash: when an identical file already
        # exists, save_file() reuses its file_url and never calls write_file().
        # The new File then points at another document's stored object and has
        # no mapping of its own, so retiring the original would break it.
        # Give it its own mapping row against the same remote path, and its own
        # URL, so the two are independent bookkeeping entries over one object.
        remote = _adopt_deduplicated(doc)
        if not remote:
            return

    # Repair the URL if write_file() ran before the name was assigned.
    expected = f"{PROXY_ROUTE}?file={frappe.utils.quoted(doc.name)}"
    if doc.file_url != expected:
        doc.db_set("file_url", expected, update_modified=False)
        doc.file_url = expected

    if frappe.db.exists("Nextcloud Stored File", {"file": doc.name}):
        return
    frappe.get_doc({
        "doctype": "Nextcloud Stored File",
        "file": doc.name,
        "remote_path": remote,
        "attached_to_doctype": doc.attached_to_doctype,
        "attached_to_name": doc.attached_to_name,
        "is_private": doc.is_private,
        "file_size": doc.file_size,
    }).insert(ignore_permissions=True)


def on_trash(doc, method=None):
    """Drop the mapping once the File itself goes.

    Runs after delete_file_data_content, which needs the mapping to find what
    to retire on the platform.
    """
    for name in frappe.get_all("Nextcloud Stored File",
                               filters={"file": doc.name}, pluck="name"):
        frappe.delete_doc("Nextcloud Stored File", name,
                          ignore_permissions=True, force=True)
