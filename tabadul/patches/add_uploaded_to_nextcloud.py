"""Existing installs: add the Uploaded To Nextcloud field, then backfill it.

after_install only reaches fresh sites. Files stored before this field existed
must read as stored, or the checkbox would claim half the truth: unticked
meaning either "on local disk" or "stored before we started recording it".
The mapping table knows which Files are remote, so backfill from it.
"""

import frappe

from tabadul.install import ensure_custom_fields


def execute():
    ensure_custom_fields()
    stored = frappe.get_all("Nextcloud Stored File", pluck="file")
    for chunk_start in range(0, len(stored), 500):
        chunk = stored[chunk_start:chunk_start + 500]
        frappe.db.set_value("File", {"name": ["in", chunk]},
                            "uploaded_to_nextcloud", 1, update_modified=False)
