import frappe
from frappe.model.document import Document


def _flag_exists():
    """The column arrives via patch on migrate. Between a code update and that
    migrate, uploads must keep working, so every write is guarded rather than
    assumed."""
    return frappe.db.has_column("File", "uploaded_to_nextcloud")


class NextcloudStoredFile(Document):
    """Where a routed attachment actually landed on the platform.

    One row per stored File. Written at upload time and never recomputed —
    the path is rendered from the attached document's fields, and those fields
    change under us.

    Every way a File comes to live on Nextcloud — upload, dedup adoption, the
    picker, migration — ends with one of these rows, so this controller is the
    single place the File's "Uploaded To Nextcloud" checkbox is kept true to
    the mapping. Setting it at each call site instead would guarantee one of
    them forgets.
    """

    def after_insert(self):
        if _flag_exists():
            frappe.db.set_value("File", self.file, "uploaded_to_nextcloud", 1,
                                update_modified=False)

    def on_trash(self):
        # The mapping usually dies because its File is dying, and writing to a
        # half-deleted File is pointless. But a mapping can also be removed on
        # its own; then the File remains, and the checkbox must not keep
        # claiming bytes that are no longer tracked.
        if _flag_exists() and frappe.db.exists("File", self.file):
            frappe.db.set_value("File", self.file, "uploaded_to_nextcloud", 0,
                                update_modified=False)
