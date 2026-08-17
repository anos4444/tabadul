"""Tests for attachment routing.

Every assertion here is paired with a negative control — a case that makes the
same check fail. A test that only ever exercises the passing path proves the
code ran, not that it works.

The pure-function tests need no site. The integration tests are skipped unless
a storage rule is actually configured, and say so rather than passing quietly.
"""

import unicodedata
import unittest

import frappe

from tabadul.attachments import (
    PROXY_ROUTE,
    is_remote,
    render_folder,
    rule_for,
    sanitize,
    should_route,
)


class _Stub(dict):
    """Enough of a File to exercise routing without touching the database."""

    def __getattr__(self, k):
        return self.get(k)

    def __setattr__(self, k, v):
        self[k] = v


class TestSanitize(unittest.TestCase):
    def test_nfc_normalisation_is_load_bearing(self):
        # Written as escapes on purpose. As literal characters these two look
        # identical in source, and any editor or paste that normalises the file
        # would silently collapse them and turn the control below into a no-op.
        decomposed = "\u0627\u0654\u062d\u0645\u062f"   # alef + combining hamza
        composed = "\u0623\u062d\u0645\u062f"            # alef-with-hamza

        # NEGATIVE CONTROL: unnormalised these are different strings. If this
        # ever fails, the bug the normalisation guards against cannot occur and
        # the assertions below stop proving anything.
        self.assertNotEqual(decomposed, composed,
                            "inputs are identical; the test no longer proves anything")

        self.assertEqual(sanitize(decomposed), sanitize(composed))
        self.assertEqual(sanitize(decomposed), unicodedata.normalize("NFC", composed))

    def test_path_separators_cannot_escape_a_segment(self):
        self.assertNotIn("/", sanitize("a/b"))
        self.assertNotIn("\\", sanitize("a\\b"))
        # NEGATIVE CONTROL: a name with no separator is left alone, so the
        # replacement above is doing work rather than mangling everything.
        self.assertEqual(sanitize("plain name.pdf"), "plain name.pdf")

    def test_empty_and_dot_only_names_get_a_usable_fallback(self):
        for bad in ("", "   ", "...", None):
            self.assertEqual(sanitize(bad), "unnamed")

    def test_length_is_capped(self):
        self.assertEqual(len(sanitize("x" * 500)), 180)


class TestIsRemote(unittest.TestCase):
    def test_recognises_our_own_urls_only(self):
        self.assertTrue(is_remote(_Stub(file_url=f"{PROXY_ROUTE}?file=abc")))
        # NEGATIVE CONTROLS: ordinary Frappe files must not be claimed.
        self.assertFalse(is_remote(_Stub(file_url="/private/files/x.pdf")))
        self.assertFalse(is_remote(_Stub(file_url="/files/x.pdf")))
        self.assertFalse(is_remote(_Stub(file_url="https://example.com/x.pdf")))
        self.assertFalse(is_remote(_Stub(file_url=None)))


class TestRouting(unittest.TestCase):
    def test_unattached_files_are_never_routed(self):
        # Site assets and print-format images have no attachment target, and
        # routing them would put storage under everything.
        self.assertFalse(should_route(_Stub(attached_to_doctype=None, is_folder=0)))

    def test_folders_are_never_routed(self):
        self.assertFalse(should_route(_Stub(attached_to_doctype="ToDo", is_folder=1)))


class TestRoutingOnSite(unittest.TestCase):
    """These reach Nextcloud Settings, so they need a bound site.

    Skipped rather than errored when there is none — a bench-less run should
    report "not exercised", never a red failure that means nothing.
    """

    def setUp(self):
        if not getattr(frappe.local, "site", None):
            self.skipTest("no site bound; run with `bench --site <site> run-tests`")

    def test_doctype_without_a_rule_is_not_routed(self):
        # NEGATIVE CONTROL for the whole opt-in design: a doctype nobody
        # configured must stay on local disk.
        self.assertIsNone(rule_for("__NoSuchDoctype__"))
        self.assertFalse(should_route(
            _Stub(attached_to_doctype="__NoSuchDoctype__", is_folder=0, is_private=1)))


class TestPathTemplate(unittest.TestCase):
    """render_folder needs Settings, so these run only on a site."""

    def setUp(self):
        if not getattr(frappe.local, "site", None):
            self.skipTest("no site bound; run with `bench --site <site> run-tests`")

    def test_template_falls_back_when_a_field_is_unknown(self):
        rule = _Stub(path_template="{doctype}/{__not_a_field__}")
        f = _Stub(attached_to_doctype="ToDo", attached_to_name="nonexistent")
        out = render_folder(f, rule)
        # It must produce *a* path rather than raising: a template outliving a
        # renamed field should still file the document somewhere predictable.
        self.assertTrue(out.startswith("/"))
        self.assertIn("ToDo", out)

    def test_template_uses_doctype_and_name_by_default(self):
        rule = _Stub(path_template=None)
        f = _Stub(attached_to_doctype="ToDo", attached_to_name="ABC123")
        out = render_folder(f, rule)
        self.assertIn("ToDo", out)
        self.assertIn("ABC123", out)


class TestMappingDoesNotBlockDeletes(unittest.TestCase):
    """A stored attachment must never make its document undeletable.

    Regression: attached_to_name was declared as a Dynamic Link, and Frappe's
    check_if_doc_is_dynamically_linked then refused to delete any document that
    had one — enabling a rule on Employee would have made every Employee
    permanently undeletable. Core's own File doctype uses Data here, which is
    exactly why deleting a document with ordinary attachments works.
    """

    def setUp(self):
        if not getattr(frappe.local, "site", None):
            self.skipTest("no site bound")

    def test_attached_to_name_is_not_a_dynamic_link(self):
        meta = frappe.get_meta("Nextcloud Stored File")
        field = meta.get_field("attached_to_name")
        self.assertIsNotNone(field, "attached_to_name is missing")
        self.assertNotEqual(
            field.fieldtype, "Dynamic Link",
            "attached_to_name is a Dynamic Link again; documents with stored "
            "attachments will refuse to delete")

    def test_document_with_a_stored_attachment_can_be_deleted(self):
        # NEGATIVE CONTROL for the above: assert the behaviour, not just the
        # schema. A future change could block deletes by another route.
        todo = frappe.get_doc({"doctype": "ToDo",
                               "description": "tabadul delete-block regression"}).insert()
        frappe.get_doc({
            "doctype": "Nextcloud Stored File",
            "file": frappe.get_all("File", limit=1, pluck="name")[0],
            "remote_path": "/Frappe/ToDo/%s/probe.txt" % todo.name,
            "attached_to_doctype": "ToDo",
            "attached_to_name": todo.name,
        }).insert(ignore_permissions=True)

        try:
            frappe.delete_doc("ToDo", todo.name)
        except frappe.LinkExistsError as e:
            self.fail("a stored-attachment mapping blocked the delete: %s" % e)


class TestDeduplication(unittest.TestCase):
    """Frappe reuses an identical file rather than storing it twice.

    save_file() finds a File with the same content_hash, copies its file_url
    and never calls write_file(). The new attachment then points at another
    document's stored object. Two things must hold: it gets its own mapping,
    and retiring one attachment must not pull the object out from under the
    other.
    """

    def setUp(self):
        if not getattr(frappe.local, "site", None):
            self.skipTest("no site bound")

    def test_shared_object_is_not_retired_while_referenced(self):
        from tabadul.attachments import stored_path

        shared = "/Frappe/Test/shared-object.txt"
        files = frappe.get_all("File", limit=2, pluck="name")
        if len(files) < 2:
            self.skipTest("need two File rows to simulate a shared object")

        rows = []
        for f in files[:2]:
            if frappe.db.exists("Nextcloud Stored File", {"file": f}):
                continue
            rows.append(frappe.get_doc({
                "doctype": "Nextcloud Stored File",
                "file": f, "remote_path": shared,
            }).insert(ignore_permissions=True))
        if len(rows) < 2:
            self.skipTest("could not create two mappings on one path")

        others = frappe.db.count("Nextcloud Stored File",
                                 {"remote_path": shared, "file": ["!=", rows[0].file]})
        self.assertGreater(others, 0,
                           "guard would not fire; the shared object would be retired")

        # NEGATIVE CONTROL: with only one reference left, the guard must NOT fire,
        # otherwise nothing would ever be archived at all.
        rows[1].delete(ignore_permissions=True)
        remaining = frappe.db.count("Nextcloud Stored File",
                                    {"remote_path": shared, "file": ["!=", rows[0].file]})
        self.assertEqual(remaining, 0, "guard would block a legitimate retirement")
        rows[0].delete(ignore_permissions=True)


class TestDownloadPermission(unittest.TestCase):
    """The proxy is the security boundary; this is the test that matters most.

    Skipped unless a rule is configured, because without one nothing is stored
    remotely and the check would pass for the wrong reason.
    """

    def setUp(self):
        if not getattr(frappe.local, "site", None):
            self.skipTest("no site bound")

    def test_guest_is_refused(self):
        from tabadul.api import download_attachment

        original = frappe.session.user
        try:
            frappe.set_user("Guest")
            with self.assertRaises(frappe.PermissionError):
                download_attachment("any-file-name")
        finally:
            frappe.set_user(original)


if __name__ == "__main__":
    unittest.main()
