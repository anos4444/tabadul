app_name = "tabadul"
app_title = "Tabadul"
app_publisher = "AAA Consulting"
app_description = "Drive a Nextcloud from Frappe: packaged, expiring, revocable external file shares."
app_email = "a.abdulla@aaacons.com"
app_license = "Proprietary"

add_to_apps_screen = [
    {
        "name": "tabadul",
        "logo": "/assets/tabadul/images/tabadul.svg",
        "title": "المشاركة الآمنة",
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
