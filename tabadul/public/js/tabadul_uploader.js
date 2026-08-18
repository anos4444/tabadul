// Register a Nextcloud source in Frappe's upload dialog.
//
// FileUploader exposes `static UploadOptions = []` and maps it into the
// component as `additional_upload_handlers`, rendering each entry as a button
// beside "Upload from device". That is a supported extension point, so this
// needs no patching of core and survives upgrades. Google Drive in the same
// dialog is hardcoded because it predates this hook — it is not the pattern
// to copy.
//
// The button attaches a file that is ALREADY on Nextcloud, by reference. It
// does not upload: an ordinary upload on a routed doctype already lands on
// Nextcloud via the write_file hook, so a second path for the same thing would
// only be a way to get it wrong.

frappe.provide("tabadul");

tabadul.NEXTCLOUD_ICON = `
<svg width="30" height="30" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
  <circle cx="16" cy="16" r="15" stroke="var(--text-color)" stroke-width="1.5" opacity="0.35"/>
  <path d="M9.5 19.5a3.5 3.5 0 0 1 .4-6.98 5 5 0 0 1 9.66-1.3A4 4 0 0 1 23.5 19.5H9.5z"
        stroke="var(--text-color)" stroke-width="1.6" stroke-linejoin="round" fill="none"/>
</svg>`;

tabadul.open_nextcloud_picker = function ({ doctype, docname, dialog, uploader }) {
	if (!doctype || !docname) {
		frappe.msgprint(__("Save the document first, then attach from Nextcloud."));
		return;
	}

	let current_path = null;

	const picker = new frappe.ui.Dialog({
		title: __("Attach from Nextcloud"),
		size: "large",
		fields: [
			{ fieldname: "here", fieldtype: "HTML" },
			{ fieldname: "listing", fieldtype: "HTML" },
			{
				fieldname: "is_private",
				fieldtype: "Check",
				label: __("Private"),
				default: 1,
				description: __("Private files are only served to users who may read this document."),
			},
		],
	});

	function render_error(message) {
		picker.fields_dict.listing.$wrapper.html(
			`<div class="text-muted" style="padding:2rem;text-align:center">
				${frappe.utils.escape_html(message)}
			 </div>`
		);
	}

	function load(path) {
		picker.fields_dict.listing.$wrapper.html(
			`<div class="text-muted" style="padding:2rem;text-align:center">${__("Loading…")}</div>`
		);

		frappe.call({
			method: "tabadul.api.browse",
			args: { path: path || null },
			callback: (r) => {
				if (!r.message) return render_error(__("No response from the server."));
				current_path = r.message.path;
				const root = r.message.root;

				picker.fields_dict.here.$wrapper.html(
					`<div class="text-muted small" style="margin-bottom:.5rem">
						${frappe.utils.escape_html(current_path)}
					 </div>`
				);

				const rows = [];
				if (current_path !== root) {
					rows.push(`<tr class="tabadul-row" data-up="1">
						<td colspan="2">&#8593; ${__("Up one level")}</td></tr>`);
				}

				(r.message.entries || []).forEach((e) => {
					const size = e.is_folder
						? ""
						: e.size == null
						? ""
						: frappe.form.formatters.FileSize(e.size);
					rows.push(`<tr class="tabadul-row"
							data-path="${frappe.utils.escape_html(e.path)}"
							data-folder="${e.is_folder ? 1 : 0}">
						<td>${e.is_folder ? "&#128193;" : "&#128196;"}
							${frappe.utils.escape_html(e.name)}</td>
						<td class="text-muted text-right">${size}</td>
					</tr>`);
				});

				if (!rows.length) {
					return render_error(__("This folder is empty."));
				}

				picker.fields_dict.listing.$wrapper.html(
					`<table class="table table-hover" style="margin:0;cursor:pointer">
						<tbody>${rows.join("")}</tbody>
					 </table>`
				);

				picker.fields_dict.listing.$wrapper.find(".tabadul-row").on("click", function () {
					const $r = $(this);
					if ($r.data("up")) {
						const parent = current_path.replace(/\/[^/]+$/, "") || root;
						return load(parent);
					}
					if (String($r.data("folder")) === "1") {
						return load($r.data("path"));
					}
					attach($r.data("path"));
				});
			},
			error: () => render_error(__("Could not reach Nextcloud.")),
		});
	}

	function attach(remote_path) {
		frappe.call({
			method: "tabadul.api.attach_remote",
			args: {
				doctype,
				docname,
				remote_path,
				is_private: picker.get_value("is_private") ? 1 : 0,
			},
			freeze: true,
			freeze_message: __("Attaching…"),
			callback: (r) => {
				if (!r.message) return;
				picker.hide();
				if (dialog) dialog.hide();
				frappe.show_alert({
					message: __("Attached {0}", [remote_path.split("/").pop()]),
					indicator: "green",
				});
				// Refresh the sidebar so the new attachment appears without a reload.
				if (cur_frm && cur_frm.doc && cur_frm.doc.name === docname) {
					cur_frm.reload_doc();
				}
			},
		});
	}

	picker.show();
	load(null);
};

frappe.after_ajax(() => {
	if (!frappe.ui || !frappe.ui.FileUploader) return;
	if (!frappe.ui.FileUploader.UploadOptions) return;

	// Guard against double registration when the bundle is evaluated twice.
	const already = frappe.ui.FileUploader.UploadOptions.some((o) => o.__tabadul);
	if (already) return;

	frappe.ui.FileUploader.UploadOptions.push({
		__tabadul: true,
		label: __("Nextcloud"),
		icon: tabadul.NEXTCLOUD_ICON,
		action: tabadul.open_nextcloud_picker,
	});
});
