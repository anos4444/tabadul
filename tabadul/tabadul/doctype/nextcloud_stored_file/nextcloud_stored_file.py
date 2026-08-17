import frappe
from frappe.model.document import Document


class NextcloudStoredFile(Document):
    """Where a routed attachment actually landed on the platform.

    One row per stored File. Written at upload time and never recomputed —
    the path is rendered from the attached document's fields, and those fields
    change under us.
    """

    pass
