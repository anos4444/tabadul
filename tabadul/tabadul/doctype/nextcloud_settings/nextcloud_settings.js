// The connection result was previously computed but never shown: the method
// existed with nothing to call it. Silence after entering credentials is the
// wrong feedback, so the status is rendered as an indicator at the top of the
// form and refreshed on demand.

frappe.ui.form.on("Nextcloud Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => {
			frm.call({
				doc: frm.doc,
				method: "test_connection",
				freeze: true,
				freeze_message: __("Contacting Nextcloud…"),
				callback: (r) => {
					if (!r.message) return;
					frappe.show_alert({
						message: r.message.message,
						indicator: r.message.ok ? "green" : "red",
					});
					frm.reload_doc();
				},
			});
		});

		show_status(frm);
	},

	storage_enabled(frm) {
		if (frm.doc.storage_enabled && !frm.doc.base_url) {
			frappe.msgprint(__("Set the server URL and service account first."));
			frm.set_value("storage_enabled", 0);
		}
	},
});

function show_status(frm) {
	const status = frm.doc.connection_status || "";
	if (!status) {
		frm.dashboard.clear_headline();
		return;
	}
	// "Connected as X" is the only phrasing the server emits on success, so a
	// green light is never shown for a failure message.
	const ok = status.indexOf(__("Connected as")) === 0 || status.indexOf("Connected as") === 0;
	frm.dashboard.set_headline_alert(
		`<div class="row">
			<div class="col-xs-12">
				<span class="indicator ${ok ? "green" : "red"}">
					${frappe.utils.escape_html(status)}
				</span>
			</div>
		 </div>`
	);
}
