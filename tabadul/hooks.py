app_name = "tabadul"
app_title = "Tabadul"
app_publisher = "AAA Consulting"
app_description = "Drive a Nextcloud from Frappe: packaged, expiring, revocable external file shares."
app_email = "anas.abdullah@gmail.com"
app_license = "MIT"

add_to_apps_screen = [
    {
        "name": "tabadul",
        "logo": "/assets/tabadul/images/tabadul.svg",
        "title": "Tabadul",
        "route": "/app/tabadul",
    }
]

# Hourly, not daily: an expiry that lands at 09:00 should not stay open until
# midnight. Reconciliation rides along so a share deleted directly in Nextcloud
# stops showing as active here within the hour.
scheduler_events = {
    "hourly": [
        "tabadul.tasks.close_expired_packages",
        "tabadul.tasks.reconcile_with_nextcloud",
    ],
}

# Registers a Nextcloud source in the upload dialog via
# frappe.ui.FileUploader.UploadOptions, which core maps into the component as
# additional_upload_handlers. A supported hook, not a patch.
app_include_js = "tabadul.bundle.js"

# --------------------------------------------------------------- attachments
# Frappe calls these in place of its own filesystem implementation when an app
# defines them — see frappe/core/doctype/file/file.py, File.save_file() and
# File.delete_file_data_content(). Routing is still opt-in per doctype inside
# attachments.py, so defining the hook does not by itself move any file.
write_file = "tabadul.attachments.write_file"
delete_file_data_content = "tabadul.attachments.delete_file_data_content"
before_write_file = "tabadul.attachments.before_write_file"

# Reading is not hookable: File.get_content() opens the local path directly,
# and every server-side consumer (print formats, email attachments) uses it.
override_doctype_class = {"File": "tabadul.overrides.file.NextcloudFile"}

doc_events = {
    "File": {
        "after_insert": "tabadul.overrides.file.after_insert",
        "on_trash": "tabadul.overrides.file.on_trash",
    },
}
