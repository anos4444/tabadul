/* Status values are the doctype's own — English, matching share_package.py's
   ACTIVE/EXPIRED/CANCELLED/DRAFT constants. They are NOT the Arabic labels a
   reader sees: those come from ar.csv at render time. Comparing against the
   translated label is how the create and cancel buttons went missing on every
   site, so keep these literals in step with the Select options. */
const DRAFT = 'Draft';
const ACTIVE = 'Active';
const EXPIRED = 'Expired';
const CANCELLED = 'Cancelled';

frappe.ui.form.on('Share Package', {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.status === DRAFT || frm.doc.status === ACTIVE) {
			frm.add_custom_button(__('Create shares'), () => create_shares(frm))
				.addClass('btn-primary');
		}
		if (frm.doc.status === ACTIVE) {
			frm.add_custom_button(__('Cancel'), () => cancel_shares(frm));
		}
		frm.add_custom_button(__('Share log'), () => audit(frm));

		if (frm.doc.status === CANCELLED) {
			const by = frm.doc.cancelled_by || '—';
			const why = frm.doc.cancel_reason
				? ' — ' + frappe.utils.escape_html(frm.doc.cancel_reason) : '';
			frm.set_intro(__('This package was cancelled by {0}', [by]) + why, 'red');
		} else if (frm.doc.status === EXPIRED) {
			frm.set_intro(
				__('This package has expired and recipients can no longer reach it.'),
				'orange');
		}
	},
});

function create_shares(frm) {
	frappe.confirm(
		__('A separate share will be created for each recipient ({0}). Continue?',
			[(frm.doc.recipients || []).length]),
		() => {
			frappe.dom.freeze(__('Creating shares…'));
			frm.call('create_shares').then((r) => {
				frappe.dom.unfreeze();
				frm.reload_doc();
				const m = r.message || {};
				if (m.passwords && Object.keys(m.passwords).length) show_passwords(m);
				else frappe.msgprint(m.message || __('Done'));
			}).catch(() => frappe.dom.unfreeze());
		});
}

/* Shown ONCE. The password is never stored on the document and never emailed
   with the link — sending both to the same inbox would collapse the two
   factors into one. */
function show_passwords(result) {
	const rows = Object.entries(result.passwords).map(([email, pw]) => `
		<tr>
			<td style="padding:6px 8px">${frappe.utils.escape_html(email)}</td>
			<td style="padding:6px 8px"><code>${frappe.utils.escape_html(pw)}</code></td>
		</tr>`).join('');

	const msg = Object.entries(result.passwords).map(([email, pw]) =>
		`${email}\n${__('Password')}: ${pw}`).join('\n\n');

	const d = new frappe.ui.Dialog({
		title: __('Passwords — shown once'),
		size: 'large',
		fields: [{ fieldtype: 'HTML', fieldname: 'body' }],
		primary_action_label: __('Copy all'),
		primary_action() {
			navigator.clipboard.writeText(msg)
				.then(() => frappe.show_alert(__('Copied')));
		},
	});
	d.fields_dict.body.$wrapper.html(`
		<div style="padding:4px 0 12px;line-height:1.9">
			<b>${frappe.utils.escape_html(result.message || '')}</b><br>
			${__('Send the password through a channel other than email — WhatsApp or SMS. These will not be shown again.')}
		</div>
		<table style="width:100%;border-collapse:collapse">
			<thead><tr style="background:var(--control-bg, #f4f5f3)">
				<th style="padding:6px 8px;text-align:start">${__('Recipient')}</th>
				<th style="padding:6px 8px;text-align:start">${__('Password')}</th>
			</tr></thead><tbody>${rows}</tbody>
		</table>`);
	d.show();
}

function cancel_shares(frm) {
	const d = new frappe.ui.Dialog({
		title: __('Cancel share'),
		fields: [{ fieldtype: 'Small Text', fieldname: 'reason',
			label: __('Reason'), reqd: 1 }],
		primary_action_label: __('Revoke access now'),
		primary_action(v) {
			d.hide();
			frappe.dom.freeze(__('Cancelling…'));
			frm.call('cancel_shares', { reason: v.reason }).then(() => {
				frappe.dom.unfreeze();
				frm.reload_doc();
				frappe.show_alert({ message: __('Access revoked'), indicator: 'green' });
			}).catch(() => frappe.dom.unfreeze());
		},
	});
	d.show();
}

function audit(frm) {
	frappe.call('tabadul.api.audit_view', { package: frm.doc.name }).then((r) => {
		const a = r.message;
		const rows = a.recipients.map((x) => `
			<tr>
				<td style="padding:6px 8px">${frappe.utils.escape_html(x.recipient || '')}</td>
				<td style="padding:6px 8px">${frappe.utils.escape_html(x.email || '')}</td>
				<td style="padding:6px 8px">${frappe.utils.escape_html(x.status || '')}</td>
				<td style="padding:6px 8px;text-align:center">${x.downloads ?? '—'}</td>
				<td style="padding:6px 8px">${frappe.utils.escape_html(x.password_sent_via || '')}</td>
			</tr>`).join('');
		const d = new frappe.ui.Dialog({ title: __('Share log'), size: 'large',
			fields: [{ fieldtype: 'HTML', fieldname: 'b' }] });
		d.fields_dict.b.$wrapper.html(`
			<table style="width:100%;border-collapse:collapse;font-size:13px">
				<thead><tr style="background:var(--control-bg, #f4f5f3)">
					<th style="padding:6px 8px;text-align:start">${__('Recipient')}</th>
					<th style="padding:6px 8px;text-align:start">${__('Email')}</th>
					<th style="padding:6px 8px;text-align:start">${__('Status')}</th>
					<th style="padding:6px 8px">${__('Downloads')}</th>
					<th style="padding:6px 8px;text-align:start">${__('Password channel')}</th>
				</tr></thead><tbody>${rows}</tbody>
			</table>
			<div style="margin-top:14px;padding:10px 12px;border-radius:8px;
				background:var(--yellow-50, #fdf6e3);color:var(--yellow-800, #7a5518);font-size:12.5px;line-height:1.9">
				${frappe.utils.escape_html(a.coverage_note)}
			</div>`);
		d.show();
	});
}
