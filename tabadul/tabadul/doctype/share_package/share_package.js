frappe.ui.form.on('Share Package', {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.status === 'مسودة' || frm.doc.status === 'نشطة') {
			frm.add_custom_button('إنشاء المشاركة', () => create_shares(frm))
				.addClass('btn-primary');
		}
		if (frm.doc.status === 'نشطة') {
			frm.add_custom_button('إلغاء', () => cancel_shares(frm));
		}
		frm.add_custom_button('سجل المشاركة', () => audit(frm));

		if (frm.doc.status === 'ملغاة') {
			frm.set_intro(`أُلغيت هذه الحزمة بواسطة ${frm.doc.cancelled_by || '—'}` +
				`${frm.doc.cancel_reason ? ' — ' + frappe.utils.escape_html(frm.doc.cancel_reason) : ''}`, 'red');
		} else if (frm.doc.status === 'منتهية') {
			frm.set_intro('انتهت صلاحية هذه الحزمة ولم يعد المستلمون قادرين على الوصول.', 'orange');
		}
	},
});

function create_shares(frm) {
	frappe.confirm(
		`سيتم إنشاء مشاركة منفصلة لكل مستلم (${(frm.doc.recipients || []).length}). متابعة؟`,
		() => {
			frappe.dom.freeze('جارٍ إنشاء المشاركات…');
			frm.call('create_shares').then((r) => {
				frappe.dom.unfreeze();
				frm.reload_doc();
				const m = r.message || {};
				if (m.passwords && Object.keys(m.passwords).length) show_passwords(m);
				else frappe.msgprint(m.message || 'تم');
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
		`${email}\nكلمة المرور: ${pw}`).join('\n\n');

	const d = new frappe.ui.Dialog({
		title: 'كلمات المرور — تُعرض مرة واحدة',
		size: 'large',
		fields: [{ fieldtype: 'HTML', fieldname: 'body' }],
		primary_action_label: 'نسخ الكل',
		primary_action() {
			navigator.clipboard.writeText(msg).then(() => frappe.show_alert('نُسخت'));
		},
	});
	d.fields_dict.body.$wrapper.html(`
		<div style="padding:4px 0 12px;line-height:1.9">
			<b>${frappe.utils.escape_html(result.message || '')}</b><br>
			أرسل كلمة المرور عبر قناة مختلفة عن البريد الإلكتروني — واتساب أو رسالة نصية.
			لن تُعرض هذه الكلمات مرة أخرى.
		</div>
		<table style="width:100%;border-collapse:collapse">
			<thead><tr style="background:var(--control-bg, #f4f5f3)">
				<th style="padding:6px 8px;text-align:start">المستلم</th>
				<th style="padding:6px 8px;text-align:start">كلمة المرور</th>
			</tr></thead><tbody>${rows}</tbody>
		</table>`);
	d.show();
}

function cancel_shares(frm) {
	const d = new frappe.ui.Dialog({
		title: 'إلغاء المشاركة',
		fields: [{ fieldtype: 'Small Text', fieldname: 'reason', label: 'السبب', reqd: 1 }],
		primary_action_label: 'إلغاء الوصول الآن',
		primary_action(v) {
			d.hide();
			frappe.dom.freeze('جارٍ الإلغاء…');
			frm.call('cancel_shares', { reason: v.reason }).then(() => {
				frappe.dom.unfreeze();
				frm.reload_doc();
				frappe.show_alert({ message: 'أُلغي الوصول', indicator: 'green' });
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
		const d = new frappe.ui.Dialog({ title: 'سجل المشاركة', size: 'large',
			fields: [{ fieldtype: 'HTML', fieldname: 'b' }] });
		d.fields_dict.b.$wrapper.html(`
			<table style="width:100%;border-collapse:collapse;font-size:13px">
				<thead><tr style="background:var(--control-bg, #f4f5f3)">
					<th style="padding:6px 8px;text-align:start">المستلم</th>
					<th style="padding:6px 8px;text-align:start">البريد</th>
					<th style="padding:6px 8px;text-align:start">الحالة</th>
					<th style="padding:6px 8px">التنزيلات</th>
					<th style="padding:6px 8px;text-align:start">قناة كلمة المرور</th>
				</tr></thead><tbody>${rows}</tbody>
			</table>
			<div style="margin-top:14px;padding:10px 12px;border-radius:8px;
				background:var(--yellow-50, #fdf6e3);color:var(--yellow-800, #7a5518);font-size:12.5px;line-height:1.9">
				${frappe.utils.escape_html(a.coverage_note)}
			</div>`);
		d.show();
	});
}
