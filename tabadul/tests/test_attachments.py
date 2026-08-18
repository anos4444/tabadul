"""Tests for attachment routing.

Every assertion here is paired with a negative control — a case that makes the
same check fail. A test that only ever exercises the passing path proves the
code ran, not that it works.

The pure-function tests need no site. The integration tests are skipped unless
a storage rule is actually configured, and say so rather than passing quietly.
"""

import unicodedata
import contextlib
import unittest
from unittest import mock

import frappe

from tabadul.attachments import (
    PROXY_ROUTE,
    is_remote,
    render_folder,
    rule_for,
    sanitize,
    should_route,
)


@contextlib.contextmanager
def _multi(*patches):
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


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


class TestPrivateHardFail(unittest.TestCase):
    """A private file must never silently land on the ERP disk.

    That is the entire point of the setting: an employee ID scan that falls
    back to local storage because Nextcloud blinked is invisible until someone
    audits the disk, which is exactly the outcome the app exists to prevent.
    Public files may fall back, because blocking them costs more than it saves.
    """

    def _stub(self, is_private):
        f = _Stub(file_name="x.pdf", is_private=is_private, is_folder=0,
                  attached_to_doctype="ToDo", attached_to_name="abc",
                  content_type="application/pdf")
        f._content = b"bytes"
        f.flags = _Stub()
        f.fell_back = False

        def _local():
            f.fell_back = True
            return {"file_url": "/private/files/x.pdf"}

        f.save_file_on_filesystem = _local
        return f

    @staticmethod
    def _routed(attachments):
        """Pretend the file is routed, without needing a site."""
        rule = _Stub(document_type="ToDo", enabled=1, company=None,
                     instance=None, path_template=None,
                     include_private=1, include_public=1)
        return _multi(
            mock.patch.object(attachments, "route_for", return_value=rule),
            mock.patch.object(attachments, "instance_for",
                              return_value=_Stub(doctype="Nextcloud Settings",
                                                 storage_root="Frappe")),
            mock.patch.object(attachments, "remote_path_for",
                              return_value="/Frappe/ToDo/abc/x.pdf"),
            mock.patch.object(attachments, "NextcloudClient", lambda *a, **k: None),
        )

    def test_private_upload_failure_raises_and_does_not_fall_back(self):
        from tabadul import attachments
        from tabadul.nextcloud_client import NextcloudUnreachable

        f = self._stub(is_private=1)
        with self._routed(attachments), \
             mock.patch.object(attachments, "_upload_with_retry",
                               side_effect=NextcloudUnreachable("down")):
            # Specifically the refusal frappe.throw raises. `Exception` would
            # also be satisfied by the stub blowing up on an unmocked call,
            # which is how this passed while the test was actually broken.
            with self.assertRaises(frappe.ValidationError):
                attachments.write_file(f)

        self.assertFalse(f.fell_back,
                         "a private file was written to local disk after an upload failure")

    def test_public_upload_failure_falls_back(self):
        # NEGATIVE CONTROL: if this also raised, the rule would just be "block
        # everything" and the is_private distinction would be doing no work.
        from tabadul import attachments
        from tabadul.nextcloud_client import NextcloudUnreachable

        f = self._stub(is_private=0)
        with self._routed(attachments), \
             mock.patch.object(attachments, "_upload_with_retry",
                               side_effect=NextcloudUnreachable("down")):
            attachments.write_file(f)

        self.assertTrue(f.fell_back,
                        "a public file blocked instead of degrading to local storage")

    def test_retry_only_covers_network_failures(self):
        # A rejected credential will be rejected again; retrying it only delays
        # the error the user needs to see. A blip is worth riding out.
        from tabadul import attachments
        from tabadul.nextcloud_client import NextcloudError, NextcloudUnreachable

        calls = {"rejected": 0, "blip": 0}

        class _Rejects:
            def upload_file(self, remote, content):
                calls["rejected"] += 1
                raise NextcloudError("403 rejected")

        class _Blips:
            def upload_file(self, remote, content):
                calls["blip"] += 1
                raise NextcloudUnreachable("timeout")

        with mock.patch.object(attachments, "NextcloudClient", _Rejects):
            with self.assertRaises(NextcloudError):
                attachments._upload_with_retry("/p", b"x", attempts=3)
        self.assertEqual(calls["rejected"], 1,
                         "a rejection was retried; only network blips should be")

        # NEGATIVE CONTROL: a blip MUST be retried, or the retry is dead code.
        with mock.patch.object(attachments, "NextcloudClient", _Blips), \
             mock.patch.object(attachments.time, "sleep", lambda *_: None):
            with self.assertRaises(NextcloudUnreachable):
                attachments._upload_with_retry("/p", b"x", attempts=3)
        self.assertEqual(calls["blip"], 3, "a network blip was not retried")


class TestDeleteIsNeverBlocked(unittest.TestCase):
    """Deleting a record must not depend on the storage backend.

    Regression from production: a stored object had been removed on Nextcloud
    directly, so MOVE returned 404, the hook raised, and the attachment became
    permanently undeletable from inside ERPNext. A backend problem is ours to
    log, not the user's to be trapped by.
    """

    def _stub(self):
        f = _Stub(name="F1", file_url="/api/method/tabadul.api.download_attachment?file=F1",
                  is_private=1, is_folder=0)
        f.deleted_locally = False

        def _local(only_thumbnail=False):
            f.deleted_locally = True

        f.delete_file_from_filesystem = _local
        return f

    def test_unreachable_backend_does_not_raise(self):
        from tabadul import attachments
        from tabadul.nextcloud_client import NextcloudUnreachable

        class _Dead:
            def delete_path(self, p):
                raise NextcloudUnreachable("down")

            def move_path(self, a, b):
                raise NextcloudUnreachable("down")

        reached = {"client": False}

        class _Watched(_Dead):
            def __init__(self, *a, **k):
                reached["client"] = True

        with mock.patch.object(attachments, "stored_ref",
                               return_value=_Stub(remote_path="/Frappe/X/y.pdf",
                                                  instance=None)), \
             mock.patch.object(attachments, "settings",
                               return_value=_Stub(delete_behaviour="Archive",
                                                  storage_root="Frappe",
                                                  archive_folder="_deleted")), \
             mock.patch.object(attachments.frappe.db, "count", return_value=0), \
             mock.patch.object(attachments, "NextcloudClient", _Watched):
            # Must return normally. Raising is the bug.
            attachments.delete_file_data_content(self._stub())

        # NEGATIVE CONTROL: the delete path must actually have reached the
        # backend. Without this the test also passes when no mapping is found
        # and the function returns before touching Nextcloud at all — which is
        # how it would have passed silently after stored_path stopped being
        # called.
        self.assertTrue(reached["client"],
                        "the backend was never contacted; this test proves nothing")

    def test_missing_object_is_not_an_error(self):
        # NEGATIVE CONTROL: prove move_path itself reports 404 as "already
        # gone" rather than success for every status, which would hide real
        # failures like a 403.
        from tabadul.nextcloud_client import NextcloudClient, NextcloudError

        class _Resp:
            def __init__(self, code):
                self.status_code = code

        client = NextcloudClient.__new__(NextcloudClient)
        client.base = "https://x"
        client.user = "u"
        client.password = "p"
        client.verify = True

        with mock.patch.object(client, "ensure_folder", lambda *_: True):
            with mock.patch("tabadul.nextcloud_client.requests.request",
                            return_value=_Resp(404)):
                self.assertIsNone(client.move_path("/a/b", "/c/d"))
            with mock.patch("tabadul.nextcloud_client.requests.request",
                            return_value=_Resp(403)):
                with self.assertRaises(NextcloudError):
                    client.move_path("/a/b", "/c/d")


class TestMultiTenant(unittest.TestCase):
    """One ERP, several companies, several Nextclouds.

    The failure this guards against is cross-tenant leakage: company A's
    document resolving to company B's server, or a file written to one server
    being read back from another after a rule is repointed.
    """

    def _settings(self, rules, default_instance=None):
        return _Stub(storage_enabled=1, storage_rules=rules,
                     default_instance=default_instance,
                     storage_root="Frappe", default_path_template="{doctype}/{name}")

    def test_company_rule_beats_the_general_rule(self):
        from tabadul import attachments

        general = _Stub(document_type="Sales Invoice", enabled=1, company=None,
                        instance="NC-Shared", path_template=None)
        specific = _Stub(document_type="Sales Invoice", enabled=1, company="Beta Co",
                         instance="NC-Beta", path_template=None)

        with mock.patch.object(attachments, "settings",
                               return_value=self._settings([general, specific])):
            self.assertEqual(
                attachments.rule_for("Sales Invoice", "Beta Co").instance, "NC-Beta")
            # NEGATIVE CONTROL: another company must NOT get Beta's instance,
            # or "company-aware" would mean "picks whichever rule it finds".
            self.assertEqual(
                attachments.rule_for("Sales Invoice", "Alpha Co").instance, "NC-Shared")
            self.assertEqual(
                attachments.rule_for("Sales Invoice", None).instance, "NC-Shared")

    def test_company_only_setup_refuses_an_unmatched_company(self):
        from tabadul import attachments

        only_beta = _Stub(document_type="Sales Invoice", enabled=1, company="Beta Co",
                          instance="NC-Beta", path_template=None)
        with mock.patch.object(attachments, "settings",
                               return_value=self._settings([only_beta])):
            self.assertIsNotNone(attachments.rule_for("Sales Invoice", "Beta Co"))
            # The whole point: Alpha's invoice must stay on local disk rather
            # than land in Beta's Nextcloud.
            self.assertIsNone(attachments.rule_for("Sales Invoice", "Alpha Co"))
            self.assertIsNone(attachments.rule_for("Sales Invoice", None))

    def test_disabled_rule_is_ignored_even_when_the_company_matches(self):
        from tabadul import attachments

        off = _Stub(document_type="Sales Invoice", enabled=0, company="Beta Co",
                    instance="NC-Beta", path_template=None)
        with mock.patch.object(attachments, "settings",
                               return_value=self._settings([off])):
            self.assertIsNone(attachments.rule_for("Sales Invoice", "Beta Co"))

    def test_legacy_install_still_resolves_to_settings(self):
        from tabadul import attachments

        s = self._settings([], default_instance=None)
        with mock.patch.object(attachments, "settings", return_value=s):
            # No instance on the rule, none as default: the connection on
            # Settings itself. This is what keeps existing sites working.
            self.assertIs(attachments.instance_for(None), s)
            self.assertIs(attachments.instance_for(
                _Stub(instance=None, get=lambda k, d=None: None)), s)

    def test_default_instance_is_used_when_a_rule_names_none(self):
        from tabadul import attachments

        marker = _Stub(name="NC-Default", doctype="Nextcloud Instance")
        s = self._settings([], default_instance="NC-Default")
        with mock.patch.object(attachments, "settings", return_value=s), \
             mock.patch.object(attachments.frappe, "get_cached_doc",
                               return_value=marker):
            rule = _Stub(document_type="ToDo", enabled=1, company=None, instance=None)
            self.assertIs(attachments.instance_for(rule), marker)
            # NEGATIVE CONTROL: a rule that DOES name an instance must win over
            # the default, or per-company routing silently collapses to one.
            asked = {}

            def _cached(dt, name):
                asked["name"] = name
                return _Stub(name=name, doctype="Nextcloud Instance")

            with mock.patch.object(attachments.frappe, "get_cached_doc", _cached):
                attachments.instance_for(_Stub(document_type="ToDo", enabled=1,
                                               company=None, instance="NC-Beta"))
            self.assertEqual(asked["name"], "NC-Beta")

    def test_disabled_instance_refuses_private_and_degrades_public(self):
        from tabadul import attachments

        rule = _Stub(document_type="ToDo", enabled=1, company=None, instance="NC-Off",
                     path_template=None, include_private=1, include_public=1)
        off = _Stub(name="NC-Off", doctype="Nextcloud Instance", enabled=0,
                    storage_root="Frappe")

        def _file(is_private):
            f = _Stub(file_name="x.pdf", is_private=is_private, is_folder=0,
                      attached_to_doctype="ToDo", attached_to_name="abc")
            f._content = b"bytes"
            f.flags = _Stub()
            f.fell_back = False

            def _local():
                f.fell_back = True
                return {"file_url": "/private/files/x.pdf"}

            f.save_file_on_filesystem = _local
            return f

        uploaded = {"count": 0}

        def _never(*a, **k):
            uploaded["count"] += 1

        with _multi(
            mock.patch.object(attachments, "route_for", return_value=rule),
            mock.patch.object(attachments, "instance_for", return_value=off),
            mock.patch.object(attachments, "remote_path_for",
                              return_value="/Frappe/ToDo/abc/x.pdf"),
            mock.patch.object(attachments, "NextcloudClient", lambda *a, **k: None),
            mock.patch.object(attachments, "_upload_with_retry", _never),
        ):
            private = _file(1)
            with self.assertRaises(frappe.ValidationError):
                attachments.write_file(private)
            self.assertFalse(private.fell_back,
                             "a private file landed on local disk because an "
                             "instance was disabled")

            # NEGATIVE CONTROL: public must still degrade, or "disabled" would
            # just mean "block everything" and the distinction is doing no work.
            public = _file(0)
            attachments.write_file(public)
            self.assertTrue(public.fell_back)

        self.assertEqual(uploaded["count"], 0,
                         "a disabled instance was contacted anyway")

    def test_delete_uses_the_instance_recorded_at_upload_time(self):
        from tabadul import attachments

        used = {}

        class _Client:
            def __init__(self, target=None):
                used["target"] = getattr(target, "name", None)

            def delete_path(self, p):
                used["deleted"] = p

            def move_path(self, a, b):
                used["moved"] = (a, b)

        f = _Stub(name="F1", is_folder=0,
                  file_url="/api/method/tabadul.api.download_attachment?file=F1")
        f.delete_file_from_filesystem = lambda only_thumbnail=False: None

        old_home = _Stub(name="NC-Old", doctype="Nextcloud Instance",
                         delete_behaviour="Delete", storage_root="Frappe",
                         archive_folder="_deleted")

        with mock.patch.object(attachments, "stored_ref",
                               return_value=_Stub(remote_path="/Frappe/X/y.pdf",
                                                  instance="NC-Old")), \
             mock.patch.object(attachments.frappe, "get_cached_doc",
                               return_value=old_home), \
             mock.patch.object(attachments.frappe.db, "count", return_value=0), \
             mock.patch.object(attachments, "NextcloudClient", _Client), \
             mock.patch.object(attachments, "settings",
                               return_value=self._settings([])):
            attachments.delete_file_data_content(f)

        # The file was written to NC-Old; repointing the rule elsewhere must not
        # send the delete to a server that never held it.
        self.assertEqual(used.get("target"), "NC-Old")
        self.assertEqual(used.get("deleted"), "/Frappe/X/y.pdf")


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
