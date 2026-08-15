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
