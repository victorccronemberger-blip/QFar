from __future__ import annotations

import unittest
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
