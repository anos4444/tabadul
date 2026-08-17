"""Move attachments that already exist on local disk up to Nextcloud.

Enabling a storage rule only affects files uploaded *after* it. Everything
already attached stays on disk until moved, and a half-migrated doctype is
confusing precisely because both states look identical in the UI.

Every entry point defaults to a dry run. The destructive option — removing the
local copy — is off by default and separate from the move, so a failed upload
can never take the only copy with it.
"""

import os

import frappe
from frappe import _

from tabadul.attachments import (
    PROXY_ROUTE,
    is_remote,
    remote_path_for,
    rule_for,
)
from tabadul.nextcloud_client import NextcloudClient


def _candidates(doctype, limit=None):
    """Local File rows attached to ``doctype`` that a rule would now route."""
    rows = frappe.get_all(
        "File",
        filters={"attached_to_doctype": doctype, "is_folder": 0},
        fields=["name", "file_name", "file_url", "is_private", "file_size",
                "attached_to_name"],
        order_by="creation asc",
        limit_page_length=limit or 0,
    )
    return [r for r in rows if r.file_url and not r.file_url.startswith(PROXY_ROUTE)]


@frappe.whitelist()
def plan(doctype, limit=None):
    """What a migration would do. Writes nothing."""
    frappe.only_for("System Manager")

    rule = rule_for(doctype)
    rows = _candidates(doctype, int(limit) if limit else None)
    total = sum(int(r.file_size or 0) for r in rows)

    sample = []
    for r in rows[:10]:
        doc = frappe.get_doc("File", r.name)
        try:
            target = remote_path_for(doc) if rule else None
        except Exception as e:
            target = f"<could not render: {e}>"
        sample.append({"file": r.name, "file_name": r.file_name, "target": target})

    return {
        "doctype": doctype,
        "rule_configured": bool(rule),
        "candidates": len(rows),
        "bytes": total,
        "sample": sample,
        # Stated plainly: without a rule nothing can move, and reporting a
        # candidate count without this would imply otherwise.
        "note": (_("No storage rule is configured for this doctype, so nothing can move.")
                 if not rule else ""),
    }


@frappe.whitelist()
def run(doctype, limit=None, delete_local=0, dry_run=1):
    """Queue the migration. Returns immediately with the job name."""
    frappe.only_for("System Manager")
    if not rule_for(doctype):
        frappe.throw(_("No storage rule is configured for {0}").format(doctype))

    job = frappe.enqueue(
        "tabadul.migrate_attachments.migrate",
        queue="long",
        timeout=10800,
        doctype=doctype,
        limit=int(limit) if limit else None,
        delete_local=int(delete_local or 0),
        dry_run=int(dry_run or 0),
    )
    return {"queued": True, "job": getattr(job, "id", None) or str(job),
            "dry_run": bool(int(dry_run or 0))}


def migrate(doctype, limit=None, delete_local=0, dry_run=0):
    """Do the work. Safe to re-run: already-migrated files are skipped."""
    rows = _candidates(doctype, limit)
    client = NextcloudClient() if not dry_run else None

    moved = skipped = failed = 0
    freed = 0
    errors = []

    for r in rows:
        doc = frappe.get_doc("File", r.name)
        if is_remote(doc):
            skipped += 1
            continue

        try:
            local_path = doc.get_full_path()
        except Exception as e:
            failed += 1
            errors.append(f"{r.name}: no local path ({e})")
            continue

        if not os.path.exists(local_path):
            # The File row outlived its bytes. Not our problem to fix here, but
            # counting it as migrated would be a lie.
            failed += 1
            errors.append(f"{r.name}: local file missing at {local_path}")
            continue

        remote = remote_path_for(doc)
        if dry_run:
            moved += 1
            continue

        try:
            with open(local_path, "rb") as f:
                content = f.read()
            client.upload_file(remote, content)
        except Exception as e:
            failed += 1
            errors.append(f"{r.name}: upload failed ({e})")
            continue

        doc.db_set("file_url", f"{PROXY_ROUTE}?file={frappe.utils.quoted(doc.name)}",
                   update_modified=False)
        if not frappe.db.exists("Nextcloud Stored File", {"file": doc.name}):
            frappe.get_doc({
                "doctype": "Nextcloud Stored File",
                "file": doc.name,
                "remote_path": remote,
                "attached_to_doctype": doc.attached_to_doctype,
                "attached_to_name": doc.attached_to_name,
                "is_private": doc.is_private,
                "file_size": doc.file_size,
            }).insert(ignore_permissions=True)

        # Only after the upload is confirmed and the mapping is recorded.
        if delete_local:
            try:
                size = os.path.getsize(local_path)
                os.remove(local_path)
                freed += size
            except OSError as e:
                errors.append(f"{r.name}: uploaded, but local copy remains ({e})")

        moved += 1
        frappe.db.commit()

    summary = {
        "doctype": doctype,
        "dry_run": bool(dry_run),
        "moved": moved,
        "skipped_already_remote": skipped,
        "failed": failed,
        "local_bytes_freed": freed,
        "errors": errors[:50],
        "errors_truncated": max(0, len(errors) - 50),
    }
    frappe.logger("tabadul").info(f"attachment migration: {summary}")
    return summary


@frappe.whitelist()
def verify(doctype, limit=None):
    """Check migrated files are actually retrievable.

    Counting rows proves the database was updated, not that the bytes arrived.
    This asks the platform whether each recorded path exists, which is the
    claim that matters.
    """
    frappe.only_for("System Manager")

    rows = frappe.get_all(
        "Nextcloud Stored File",
        filters={"attached_to_doctype": doctype},
        fields=["name", "file", "remote_path"],
        limit_page_length=int(limit) if limit else 0,
    )
    client = NextcloudClient()

    present = missing = 0
    missing_paths = []
    for r in rows:
        if client.path_exists(r.remote_path):
            present += 1
        else:
            missing += 1
            if len(missing_paths) < 25:
                missing_paths.append({"file": r.file, "remote_path": r.remote_path})

    return {
        "doctype": doctype,
        "checked": len(rows),
        "present_on_nextcloud": present,
        "missing_on_nextcloud": missing,
        "missing_sample": missing_paths,
        "verdict": "PASS" if rows and not missing else ("FAIL" if missing else "NOTHING TO CHECK"),
    }
