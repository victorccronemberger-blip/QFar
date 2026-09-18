from __future__ import annotations

import unittest
import os
from contextlib import ExitStack
from unittest import mock

from moneymin import hostinger_mail
from moneymin.web import server


class HostingerRoutingTests(unittest.TestCase):
    def setUp(self):
        self.profiles = [
            {
                "id": "alpha", "name": "Caixa Alpha",
                "token": "token-alpha-000000", "mailbox_id": "mailbox-alpha",
                "routes": ["alpha.example"],
            },
            {
                "id": "beta", "name": "Caixa Beta",
                "token": "token-beta-000000", "mailbox_id": "mailbox-beta",
                "routes": ["beta.example"],
            },
        ]
        self.profiles_patch = mock.patch.object(
            hostinger_mail.config, "HOSTINGER_MAIL_PROFILES", self.profiles)
        self.legacy_patch = mock.patch.object(
            hostinger_mail.config, "HOSTINGER_MAIL_TOKEN", "")
        self.profiles_patch.start()
        self.legacy_patch.start()

    def tearDown(self):
        self.legacy_patch.stop()
        self.profiles_patch.stop()

    def test_destination_domain_selects_the_right_api(self):
        calls: list[tuple[str, str]] = []

        def request(path, method="GET", body=None, *, token=None):
            calls.append((str(token), path))
            if "messages/search" in path:
                return 200, {"data": [{
                    "uid": 7,
                    "subject": "123456 is your verification code",
                    "to": [{"address": "person@beta.example"}],
                }]}
            return 204, {}

        with mock.patch.object(hostinger_mail, "_request", side_effect=request):
            code = hostinger_mail.wait_for_code(
                "person@beta.example", sender="crowtado.com", poll=0)

        self.assertEqual(code, "123456")
        self.assertTrue(calls)
        self.assertEqual({token for token, _ in calls}, {"token-beta-000000"})

    def test_mailbox_address_and_domain_are_discovered_from_hostinger(self):
        response = {"data": {"mailboxes": [{
            "resourceId": "mailbox-auto",
            "address": "Codes@Example.COM",
        }]}}
        with mock.patch.object(hostinger_mail, "_request", return_value=(200, response)):
            detected = hostinger_mail.discover_mailboxes("valid-token-000000")

        self.assertEqual(detected, [{
            "resource_id": "mailbox-auto",
            "address": "codes@example.com",
            "domain": "example.com",
        }])

    def test_unmapped_destination_falls_back_across_all_connections(self):
        searched: list[str] = []

        def request(path, method="GET", body=None, *, token=None):
            if "messages/search" in path:
                searched.append(str(token))
                messages = [] if token == "token-alpha-000000" else [{
                    "uid": 9,
                    "subject": "654321 is your verification code",
                    "to": [{"address": "person@unknown.example"}],
                }]
                return 200, {"data": messages}
            return 204, {}

        with mock.patch.object(hostinger_mail, "_request", side_effect=request):
            code = hostinger_mail.wait_for_code(
                "person@unknown.example", sender="crowtado.com", poll=0)

        self.assertEqual(code, "654321")
        self.assertEqual(searched, ["token-alpha-000000", "token-beta-000000"])

    def test_each_mailbox_keeps_its_own_uid_cursor(self):
        def request(path, method="GET", body=None, *, token=None):
            if "messages/search" not in path:
                return 204, {}
            uid = 100 if token == "token-alpha-000000" else 4
            return 200, {"data": [{"uid": uid}]}

        with mock.patch.object(hostinger_mail, "_request", side_effect=request):
            cursor = hostinger_mail.max_uid()

        self.assertEqual(cursor, {"alpha": 100, "beta": 4})


class HostingerDomainRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ))
        self.profile = {"id": "saved", "name": "Caixa antiga",
                        "token": "fixture-token-00000", "mailbox_id": "box-a", "routes": []}
        self.secure = {"hostinger": {"profiles": [self.profile]}}
        for name, value in (("HOSTINGER_MAIL_PROFILES", [self.profile]),
                            ("HOSTINGER_MAIL_TOKEN", ""), ("HOSTINGER_MAILBOX_ID", "")):
            self.stack.enter_context(mock.patch.object(server.config, name, value))
        self.stack.enter_context(mock.patch.object(server, "_migrate_legacy_integrations", return_value=self.secure))
        self.save = self.stack.enter_context(mock.patch.object(server, "save_secure_settings"))
        self.discover = self.stack.enter_context(mock.patch.object(hostinger_mail, "discover_mailboxes", return_value=[
            {"resource_id": "box-a", "address": "codes@alpha.example", "domain": "alpha.example"},
            {"resource_id": "box-b", "address": "codes@beta.example", "domain": "beta.example"},
        ]))
        self.client = server.create_app().test_client()

    def test_saved_token_without_routes_recovers_selected_mailbox_and_persists(self):
        response = self.client.get("/api/accounts/domains")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([d["domain"] for d in response.json["domains"]], ["alpha.example"])
        self.assertTrue(response.json["hostinger_configured"])
        self.assertNotIn(self.profile["token"], response.get_data(as_text=True))
        self.save.assert_called_once()
        self.client.get("/api/accounts/domains")
        self.discover.assert_called_once_with(self.profile["token"])

    def test_legacy_token_without_mailbox_preserves_correct_domain_routing(self):
        self.profile["mailbox_id"] = ""
        response = self.client.get("/api/accounts/domains")
        self.assertEqual(len(response.json["domains"]), 2)
        profiles = self.secure["hostinger"]["profiles"]
        self.assertEqual([(p["mailbox_id"], p["routes"]) for p in profiles],
                         [("box-a", ["alpha.example"]), ("box-b", ["beta.example"])])
        self.assertEqual(len({p["id"] for p in profiles}), 2)

    def test_network_failure_reports_safe_message_and_allows_retry(self):
        self.discover.side_effect = hostinger_mail.MailError("network fixture-token-00000")
        response = self.client.get("/api/accounts/domains")
        self.assertEqual(response.json["domains"], [])
        self.assertTrue(response.json["warning"])
        self.assertNotIn(self.profile["token"], response.get_data(as_text=True))
        self.save.assert_not_called()
        self.discover.side_effect = None
        self.assertEqual(len(self.client.get("/api/accounts/domains").json["domains"]), 1)

    def test_existing_routes_are_not_overwritten_or_queried(self):
        self.profile["routes"] = ["custom.example"]
        response = self.client.get("/api/accounts/domains")
        self.assertEqual(response.json["domains"][0]["domain"], "custom.example")
        self.discover.assert_not_called()
        self.save.assert_not_called()

    def test_missing_saved_mailbox_does_not_select_another_mailbox(self):
        self.profile["mailbox_id"] = "removed-box"
        response = self.client.get("/api/accounts/domains")
        self.assertEqual(response.json["domains"], [])
        self.assertTrue(response.json["warning"])
        self.save.assert_not_called()

    def test_incomplete_existing_api_can_be_identified_again(self):
        with mock.patch.object(server, "_integration_snapshot", return_value={}):
            response = self.client.put("/api/integrations/hostinger", json={
                "auto_detect": True, "token": self.profile["token"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.secure["hostinger"]["profiles"][0]["id"], "saved")

    def test_fresh_install_lists_domains_immediately_after_connecting(self):
        self.secure["hostinger"]["profiles"] = []
        server.config.HOSTINGER_MAIL_PROFILES = []
        self.assertEqual(self.client.get("/api/accounts/domains").json["domains"], [])
        with mock.patch.object(server, "_integration_snapshot", return_value={}):
            response = self.client.put("/api/integrations/hostinger", json={
                "auto_detect": True, "token": self.profile["token"]})
        self.assertEqual(response.status_code, 200)
        domains = self.client.get("/api/accounts/domains").json["domains"]
        self.assertEqual([d["domain"] for d in domains], ["alpha.example", "beta.example"])
        self.assertNotIn(self.profile["token"], str(domains))


class HostingerProfilesApiTests(unittest.TestCase):
    def test_same_api_token_cannot_be_registered_twice(self):
        secure = {"hostinger": {"profiles": [{
            "id": "existing", "name": "codes@example.com",
            "token": "same-token-0000000", "mailbox_id": "box-one",
            "routes": ["example.com"],
        }]}}
        client = server.create_app().test_client()
        with mock.patch.object(server, "_migrate_legacy_integrations", return_value=secure), \
             mock.patch.object(server.hostinger_mail, "discover_mailboxes") as discover:
            response = client.put("/api/integrations/hostinger", json={
                "auto_detect": True,
                "token": "same-token-0000000",
            })

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "hostinger_already_connected")
        discover.assert_not_called()
        self.assertEqual(len(secure["hostinger"]["profiles"]), 1)

    def test_token_automatically_imports_mailboxes_and_domains(self):
        secure = {"hostinger": {"profiles": [{
            "id": "existing", "name": "Antiga",
            "token": "old-token-00000000", "mailbox_id": "old-box",
            "routes": ["old.example"],
        }]}}
        detected = [
            {"resource_id": "box-a", "address": "codes@alpha.example",
             "domain": "alpha.example"},
            {"resource_id": "box-b", "address": "codes@beta.example",
             "domain": "beta.example"},
        ]
        client = server.create_app().test_client()
        with mock.patch.object(server, "_migrate_legacy_integrations", return_value=secure), \
             mock.patch.object(server, "save_secure_settings"), \
             mock.patch.object(server, "_integration_snapshot", return_value={}), \
             mock.patch.object(server.hostinger_mail, "discover_mailboxes",
                               return_value=detected), \
             mock.patch.object(server.config, "HOSTINGER_MAIL_PROFILES", []):
            response = client.put("/api/integrations/hostinger", json={
                "auto_detect": True,
                "token": "new-token-00000000",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["detected_count"], 2)
        profiles = secure["hostinger"]["profiles"]
        self.assertEqual(len(profiles), 3)
        self.assertEqual(profiles[1]["name"], "codes@alpha.example")
        self.assertEqual(profiles[1]["mailbox_id"], "box-a")
        self.assertEqual(profiles[1]["routes"], ["alpha.example"])

    def test_new_connection_is_added_without_replacing_existing_one(self):
        secure = {"hostinger": {"profiles": [{
            "id": "existing", "name": "Primeira",
            "token": "existing-token-0000", "mailbox_id": "box-one",
            "routes": ["one.example"],
        }]}}
        client = server.create_app().test_client()
        with mock.patch.object(server, "_migrate_legacy_integrations", return_value=secure), \
             mock.patch.object(server, "save_secure_settings"), \
             mock.patch.object(server, "_integration_snapshot", return_value={}), \
             mock.patch.object(server.hostinger_mail, "test_connection",
                               return_value={"ok": True, "mailboxes": 1}), \
             mock.patch.object(server.config, "HOSTINGER_MAIL_PROFILES", []):
            response = client.put("/api/integrations/hostinger", json={
                "create": True,
                "name": "Segunda",
                "token": "second-token-00000",
                "mailbox_id": "box-two",
                "routes": ["two.example"],
            })

        self.assertEqual(response.status_code, 200)
        profiles = secure["hostinger"]["profiles"]
        self.assertEqual(len(profiles), 2)
        self.assertEqual([item["name"] for item in profiles], ["Primeira", "Segunda"])
        self.assertEqual(profiles[1]["routes"], ["two.example"])


if __name__ == "__main__":
    unittest.main()
