frappe.ui.form.on("Nextcloud Instance", {
	refresh(frm) {
		if (!frm.is_new()) {
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
		}

		const status = frm.doc.connection_status || "";
		if (status) {
			const ok = status.indexOf("Connected as") === 0 || status.indexOf(__("Connected as")) === 0;
			frm.dashboard.set_headline_alert(
				`<div class="row"><div class="col-xs-12">
					<span class="indicator ${ok ? "green" : "red"}">
						${frappe.utils.escape_html(status)}
					</span>
				 </div></div>`
			);
		}
	},
});
