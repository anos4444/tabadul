"""Custom fields tabadul adds to core doctypes.

Frappe's own cloud integrations mark a File with "Uploaded To Dropbox" and
"Uploaded To Google Drive" — read-only checkboxes on the form, filterable in
the list view. This is the same signal for Nextcloud. Without it the only way
to tell where a File's bytes live is to read its URL.

Kept in one place and called from both after_install and a patch, so a fresh
install and an upgrade cannot drift apart.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# Mirrors the shape of core's own uploaded_to_* fields, and sits right after
# them on the form. Both v15 and v16 carry uploaded_to_google_drive, so the
# anchor is safe on every supported version.
CUSTOM_FIELDS = {
    "File": [
        {
            "fieldname": "uploaded_to_nextcloud",
            "label": "Uploaded To Nextcloud",
            "fieldtype": "Check",
            "default": "0",
            "read_only": 1,
            "no_copy": 1,
            "insert_after": "uploaded_to_google_drive",
        },
    ],
}


def ensure_custom_fields():
    """Idempotent: create_custom_fields updates in place on re-run."""
    create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)


def after_install():
    ensure_custom_fields()
