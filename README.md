# tabadul — تبادل

A thin Frappe orchestration layer over a Nextcloud instance. It creates
packaged, password-protected, expiring, revocable shares for external
recipients, and reads back what the platform reports.

**It is deliberately client-neutral.** It carries no customer, sector or
domain vocabulary — no organisation names, no asset types. Anything specific
belongs in the app that *uses* tabadul, linked through `linked_doctype` /
`linked_name`. Keep it that way; the same rule applies here as to a Desk shell.

## What it does

- **Nextcloud Settings** — one place for the base URL, service account and its
  app-password (stored encrypted, never sent to the browser).
- **Share Package** — a titled bundle of paths shared with a list of named
  recipients. One Nextcloud share per recipient, so access is separable and
  revocable per person.
- **Expiry and revocation** — an hourly job closes packages past their date and
  reconciles against Nextcloud, so a share deleted directly in Nextcloud does
  not keep showing as active here.

## Attachment storage

The second thing tabadul does. Share Package hands files *out*; this decides
where a document's attachments *live*.

- **Opt-in per doctype.** `Nextcloud Settings → تخزين المرفقات` lists the
  doctypes whose attachments go to Nextcloud. Everything not listed stays on
  local disk, and a site with the app installed but no rules behaves exactly
  like one without it.
- **Path templates.** `{doctype}/{name}` by default; a rule may override with
  any field of the attached document, e.g.
  `Employee/{employee_number} - {employee_name}`.
- **Deletion is configurable** — `Archive` (move under `_deleted/`, the
  default), `Delete`, or `Keep`. HR records are not a good place to discover
  that a mistaken click was final.

### How it hooks in

Frappe supports this properly, so no core patching:

| Path | Mechanism |
|---|---|
| Write | `write_file` hook — Frappe calls it instead of `save_file_on_filesystem()` |
| Delete | `delete_file_data_content` hook |
| Read | `override_doctype_class` on File — **reading is not hookable**; `get_content()` opens the local path directly, and print formats and email attachments all use it |

### Private files never fall back to local disk

If an upload cannot reach Nextcloud:

- **Private** — the upload **fails**. Nothing is written to the ERP server.
- **Public** — falls back to local disk and is logged;
  `migrate_attachments.plan()` lists it later as a candidate to move.

The distinction follows the sensitivity of the file rather than an
administrator's guess about a doctype. An employee ID scan must never touch the
ERP disk; a product image landing there during an outage harms nobody, and
blocking it would cost more than it saves.

Network failures are retried three times before this applies, so a brief blip
does not block anyone. A rejected credential is **not** retried — it will be
rejected again, and the person needs the error now.

The consequence, stated plainly: while Nextcloud is unreachable, staff cannot
attach private documents at all. That is the setting working, not failing.

### Several companies, several Nextclouds

One ERP often serves more than one company, and those companies do not always
share a file server. **Nextcloud Instance** is a full doctype — listable,
creatable, deletable — holding one server and the account tabadul uses on it.
A storage rule may name an instance and a company:

| Doctype | Company | Nextcloud | Effect |
|---|---|---|---|
| Sales Invoice | *(blank)* | NC-Shared | every company's invoices, unless overridden |
| Sales Invoice | Beta Co | NC-Beta | Beta's invoices only |
| Employee | Beta Co | NC-Beta-HR | one company, a second server |

Resolution order for a document: **the rule matching its company**, then the
general rule for its doctype, then no routing at all. That last step is
deliberate — where only company-specific rules exist and none match, the file
stays on local disk rather than landing in another company's Nextcloud. A
routing mistake that crosses tenants is worse than one that doesn't route.

Two things are recorded rather than recomputed, for the same reason paths are:

- **The instance is written onto the mapping at upload time.** Repointing a
  rule at a new server later does not send reads, deletes or verification
  hunting for old files somewhere they were never written.
- **A disabled instance refuses private uploads rather than falling back.**
  Unticking *Enabled* is deliberate, but it must not quietly become a route
  onto the ERP disk; it behaves exactly like an unreachable server.

An instance cannot be deleted while `Nextcloud Stored File` rows point at it,
while a storage rule still targets it, or while it is the site default.

**Nothing about this is required.** With no instance defined, every rule
resolves to the connection on `Nextcloud Settings` itself, exactly as before —
existing installs need no data patch, and older mappings with a blank instance
keep resolving to that same default.

Company-based routing reads the `Company` link, so it needs ERPNext installed.
Everything else here works on plain Frappe.

### Attaching a file already on Nextcloud

The upload dialog gains a **Nextcloud** button beside "Upload from device". It
attaches an existing Nextcloud file *by reference* — no re-upload, no second
copy. Ordinary uploads on a routed doctype already land on Nextcloud through
the storage hook, so a second upload path would only be a way to get it wrong.

It is **off by default** — `Nextcloud Settings → Allow attaching existing
Nextcloud files`. Browsing the service account's folder tree is a wider
capability than attaching a file, and ordinary uploads already reach Nextcloud
without it. The picker browses the instance the *document* is routed to, so it
cannot offer a file from a server that document could never read back from.

This uses `frappe.ui.FileUploader.UploadOptions`, a supported registry that
core maps into the dialog as `additional_upload_handlers`. Nothing is patched,
so a Frappe upgrade cannot break the dialog. Google Drive in that same dialog
is hardcoded because it predates the hook — it is not the pattern to copy.

### The download proxy is the security boundary

Frappe's `/private/files/` route refuses to serve bytes the session user may
not see. Once the bytes live on Nextcloud that check no longer runs, so
`api.download_attachment` reimplements it. It checks permission on the
**attached document**, not on the File row — a File is readable by more people
than the document it hangs off, and using the File's own permission would
quietly widen access.

### Trying it

Supports **Frappe v15 and v16**, declared in `pyproject.toml` under
`[tool.bench.frappe-dependencies]`. Frappe Cloud refuses an app without that
key, and both versions carry the storage seam this app hooks into.

```bash
bench get-app https://github.com/anos4444/tabadul.git
bench --site <site> install-app tabadul
bench --site <site> migrate
```

Then, in `Nextcloud Settings`:

1. Fill in the connection (server URL, service account, app password) and hit
   **Test Connection**.
2. Tick **Store attachments on Nextcloud**.
3. Add a row under **Included doctypes** — start with one low-risk doctype,
   not Employee. `ToDo` is ideal: attach a file to a ToDo and watch it land in
   Nextcloud under `Frappe/ToDo/<name>/`.
4. Open the attachment from the document to confirm the download proxy serves
   it, then check `Nextcloud Stored File` for the mapping row.

Nothing moves until step 2 and 3 are both done. A site with the app installed
and no rules behaves exactly like one without it.

**Existing attachments stay on disk.** A rule only affects new uploads. To move
what is already there:

```python
# dry run first — writes nothing, tells you what would move and where
frappe.call("tabadul.migrate_attachments.plan", doctype="ToDo")

# then, still non-destructive: uploads but keeps the local copy
frappe.call("tabadul.migrate_attachments.run", doctype="ToDo", dry_run=0)

# prove the bytes actually arrived, not just that rows changed
frappe.call("tabadul.migrate_attachments.verify", doctype="ToDo")
```

`delete_local=1` is a separate, deliberate step. Never pass it in the same run
that uploads.

### Tests

```bash
bench --site <site> run-tests --app tabadul
```

Pure-function tests run anywhere; the rest skip with a stated reason when no
site is bound rather than failing meaninglessly.

### Two things that will bite you

- **Nextcloud normalises filenames to NFC.** An alef with hamza written as
  U+0627 U+0654 comes back as U+0623. Compare paths without normalising both
  sides and identical files look missing. `attachments.sanitize()` handles it.
- **Stored paths are recorded, not recomputed.** A path rendered from
  `{employee_name}` changes the moment someone is renamed; recomputing later
  would look in a folder that no longer exists. `Nextcloud Stored File` keeps
  what was actually written.

## What it deliberately does NOT claim

Nextcloud reports share creation, share deletion, authentication, and download
**counts**. It does not provide a per-recipient download log, and anonymous
views of a share page are not reliably recorded. The audit view shows exactly
what the platform captures and nothing more — do not add UI or reports that
imply per-person download tracking.

## Password delivery

The link and the password must not travel the same channel, or the two factors
collapse into one. Nextcloud emails the link; tabadul shows the password once,
formatted for the operator to send by another route.
