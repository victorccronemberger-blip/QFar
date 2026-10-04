"""Artificial account profiles: ownership, exact legacy migration and preservation."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import config, device_profile as profiles


class DeviceProfileOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="profile-owner-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "device_state"
        self.state.mkdir()
        patcher = patch.object(config, "DATA_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        cache = patch.object(profiles, "_cache", {})
        cache.start()
        self.addCleanup(cache.stop)
        self.owner = "a.b@example.invalid"
        self.other = "a_b@example.invalid"

    def profile(self, owner=None, *, device_id="android.ssaid:0011223344556677"):
        return profiles.DeviceProfile(
            email=owner or self.owner, device_id=device_id,
            calib={"distortion_model": "fixture-only"},
            created_wall_ms=1_600_000_000_000, boot_wall_ms=1_599_900_000_000,
        )

    def canonical(self, owner=None):
        key = (owner or self.owner).strip().casefold()
        return self.state / ("device_" + hashlib.sha256(key.encode()).hexdigest() + ".json")

    def legacy(self, owner=None):
        key = owner or self.owner
        return self.state / ("device_" + key.replace("@", "_at_").replace(".", "_") + ".json")

    def write(self, path, profile=None):
        payload = json.dumps((profile or self.profile()).to_dict(), indent=3).encode("utf-8") + b"\n"
        path.write_bytes(payload)
        return payload

    def assert_private_rejection(self, operation):
        with self.assertRaises(ValueError) as error:
            operation()
        diagnostic = str(error.exception)
        self.assertNotIn(self.owner, diagnostic)
        self.assertNotIn(self.other, diagnostic)
        self.assertNotIn("fixture-canary", diagnostic)

    def test_full_owner_hash_separates_colliding_legacy_names(self):
        self.assertEqual(self.legacy(self.owner), self.legacy(self.other))
        self.assertEqual(profiles._profile_path(self.owner), self.canonical())
        self.assertNotEqual(profiles._profile_path(self.owner), profiles._profile_path(self.other))

    def test_legacy_wrong_owner_is_rejected_without_reassignment_or_generation(self):
        payload = self.write(self.legacy(), self.profile())
        with patch.object(profiles, "_create_profile") as create:
            self.assert_private_rejection(lambda: profiles.get_profile(self.other))
        create.assert_not_called()
        self.assertEqual(self.legacy().read_bytes(), payload)
        self.assertFalse(self.canonical(self.other).exists())
        self.assertNotIn(self.other, profiles._cache)

    def test_valid_legacy_is_migrated_without_rewriting_any_bytes_or_identity(self):
        payload = self.write(self.legacy())
        with patch.object(profiles, "_create_profile") as create:
            found = profiles.get_profile(self.owner)
        create.assert_not_called()
        self.assertEqual(found.to_dict(), self.profile().to_dict())
        self.assertEqual(self.legacy().read_bytes(), payload)
        self.assertEqual(self.canonical().read_bytes(), payload)

    def test_case_and_surrounding_space_share_one_owner_and_cache_entry(self):
        self.write(self.canonical(), self.profile("A.B@EXAMPLE.INVALID"))
        first = profiles.get_profile(" A.B@EXAMPLE.INVALID ")
        second = profiles.get_profile(self.owner)
        self.assertIs(first, second)
        self.assertEqual(list(profiles._cache), [self.owner])

    def test_primary_wrong_owner_is_authoritative_even_with_valid_legacy(self):
        payload = self.write(self.canonical(), self.profile(self.other))
        old = self.write(self.legacy())
        with patch.object(profiles, "_create_profile") as create:
            self.assert_private_rejection(lambda: profiles.get_profile(self.owner))
        create.assert_not_called()
        self.assertEqual(self.canonical().read_bytes(), payload)
        self.assertEqual(self.legacy().read_bytes(), old)

    def test_corrupt_primary_never_falls_back_or_recreates(self):
        old = self.write(self.legacy())
        for payload in (b'{"fixture-canary":', b"[]", b"null", b"\xff"):
            with self.subTest(payload=payload):
                profiles._cache.clear()
                self.canonical().write_bytes(payload)
                with patch.object(profiles, "_create_profile") as create:
                    self.assert_private_rejection(lambda: profiles.get_profile(self.owner))
                create.assert_not_called()
                self.assertEqual(self.canonical().read_bytes(), payload)
                self.assertEqual(self.legacy().read_bytes(), old)

    def test_incompatible_legacy_is_preserved_instead_of_rotating_identity(self):
        payload = self.write(self.legacy(), self.profile(device_id="legacy-id"))
        with patch.object(profiles, "_create_profile") as create:
            self.assert_private_rejection(lambda: profiles.get_profile(self.owner))
        create.assert_not_called()
        self.assertEqual(self.legacy().read_bytes(), payload)
        self.assertFalse(self.canonical().exists())

    def test_cached_owner_does_not_hide_a_later_primary_owner_conflict(self):
        self.write(self.canonical())
        profiles.get_profile(self.owner)
        payload = self.write(self.canonical(), self.profile(self.other))
        self.assert_private_rejection(lambda: profiles.get_profile(self.owner))
        self.assertEqual(self.canonical().read_bytes(), payload)

    def test_legacy_case_filename_is_discovered_by_payload_owner(self):
        mixed = self.legacy("A.B@EXAMPLE.INVALID")
        payload = self.write(mixed, self.profile("A.B@EXAMPLE.INVALID"))
        found = profiles.get_profile(self.owner)
        self.assertEqual(found.device_id, self.profile().device_id)
        self.assertEqual(mixed.read_bytes(), payload)
        self.assertEqual(self.canonical().read_bytes(), payload)

    def test_disagreeing_legacy_copies_do_not_publish_a_primary(self):
        first = self.write(self.legacy())
        mixed = self.state / "device_duplicate_fixture.json"
        second = self.write(mixed, self.profile(device_id="android.ssaid:8899aabbccddeeff"))
        self.assert_private_rejection(lambda: profiles.get_profile(self.owner))
        self.assertFalse(self.canonical().exists())
        self.assertEqual(self.legacy().read_bytes(), first)
        self.assertEqual(mixed.read_bytes(), second)

    def test_existing_canonical_is_preserved_when_persist_owner_is_wrong(self):
        payload = self.write(self.canonical(), self.profile(self.other))
        self.assert_private_rejection(self.profile()._persist)
        self.assertEqual(self.canonical().read_bytes(), payload)

    def test_persist_write_failure_preserves_existing_bytes_and_reports_failure(self):
        payload = self.write(self.canonical())
        profile = self.profile()
        profile.created_wall_ms += 100
        with patch.object(profiles, "save_json", side_effect=OSError("fixture-canary"), create=True):
            self.assert_private_rejection(profile._persist)
        self.assertEqual(self.canonical().read_bytes(), payload)

    def test_first_creation_failure_never_caches_an_unpersisted_profile(self):
        with patch.object(profiles, "_create_profile", return_value=self.profile()), \
             patch.object(profiles.os if hasattr(profiles, "os") else profiles, "link", side_effect=OSError("fixture-canary"), create=True):
            self.assert_private_rejection(lambda: profiles.get_profile(self.owner))
        self.assertFalse(self.canonical().exists())
        self.assertNotIn(self.owner, profiles._cache)

    def test_profile_age_validates_owner_and_does_not_modify_legacy(self):
        payload = self.write(self.legacy())
        with patch.object(profiles, "_wall_ms_now", return_value=1_600_086_400_000):
            self.assertEqual(profiles.profile_age_days(self.owner), 1.0)
        self.assertFalse(self.canonical().exists())
        self.assertEqual(self.legacy().read_bytes(), payload)
        self.assert_private_rejection(lambda: profiles.profile_age_days(self.other))

    def test_invalid_owner_is_rejected_before_storage_access(self):
        for owner in (None, 42, "", "../fixture@example.invalid", "fixture-canary", "a@b", "a\x00@b.invalid"):
            with self.subTest(owner=owner), patch.object(profiles, "_state_dir") as state:
                self.assert_private_rejection(lambda: profiles.get_profile(owner))
                state.assert_not_called()

    def test_new_colliding_owners_have_separate_files_objects_and_states(self):
        with patch.object(profiles, "_create_profile", side_effect=lambda email, **_: self.profile(email)) as create:
            first = profiles.get_profile(self.owner)
            second = profiles.get_profile(self.other)
        self.assertEqual(create.call_count, 2)
        self.assertIsNot(first, second)
        self.assertEqual(first.email, self.owner)
        self.assertEqual(second.email, self.other)
        self.assertEqual(json.loads(self.canonical().read_bytes())["email"], self.owner)
        self.assertEqual(json.loads(self.canonical(self.other).read_bytes())["email"], self.other)

    def test_loaded_object_cannot_rebind_owner_and_persist_another_account(self):
        payload = self.write(self.canonical())
        profile = profiles.get_profile(self.owner)
        profile.email = self.other
        self.assert_private_rejection(profile._persist)
        self.assertEqual(self.canonical().read_bytes(), payload)
        self.assertFalse(self.canonical(self.other).exists())

    def test_semantically_equal_legacy_copies_accept_normalized_owner(self):
        payload = self.write(self.legacy())
        duplicate = self.state / "device_duplicate_fixture.json"
        other = self.write(duplicate, self.profile("A.B@EXAMPLE.INVALID"))
        found = profiles.get_profile(self.owner)
        self.assertEqual(found.device_id, self.profile().device_id)
        self.assertEqual(self.canonical().read_bytes(), payload)
        self.assertEqual(self.legacy().read_bytes(), payload)
        self.assertEqual(duplicate.read_bytes(), other)

    def test_exclusive_publication_rejects_concurrent_different_owner(self):
        payload = self.write(self.legacy())
        concurrent = self.profile(self.other)
        race_bytes = json.dumps(concurrent.to_dict()).encode()
        def race_link(source, target):
            Path(target).write_bytes(race_bytes)
            raise FileExistsError("fixture-canary")
        with patch.object(profiles.os, "link", side_effect=race_link):
            self.assert_private_rejection(lambda: profiles.get_profile(self.owner))
        self.assertEqual(self.canonical().read_bytes(), race_bytes)
        self.assertEqual(self.legacy().read_bytes(), payload)
        self.assertFalse(list(self.state.glob("*.tmp")))
        self.assertNotIn(self.owner, profiles._cache)

    def test_exclusive_publication_rejects_concurrent_different_identity(self):
        payload = self.write(self.legacy())
        concurrent = self.profile(device_id="android.ssaid:8899aabbccddeeff")
        race_bytes = json.dumps(concurrent.to_dict()).encode()
        def race_link(source, target):
            Path(target).write_bytes(race_bytes)
            raise FileExistsError("fixture-canary")
        with patch.object(profiles.os, "link", side_effect=race_link):
            self.assert_private_rejection(lambda: profiles.get_profile(self.owner))
        self.assertEqual(self.canonical().read_bytes(), race_bytes)
        self.assertEqual(self.legacy().read_bytes(), payload)
        self.assertFalse(list(self.state.glob("*.tmp")))

    def test_exclusive_publication_accepts_equivalent_concurrent_record_without_replacing_it(self):
        payload = self.write(self.legacy())
        race_bytes = json.dumps(self.profile("A.B@EXAMPLE.INVALID").to_dict(), indent=5).encode()
        def race_link(source, target):
            Path(target).write_bytes(race_bytes)
            raise FileExistsError("fixture-canary")
        with patch.object(profiles.os, "link", side_effect=race_link):
            found = profiles.get_profile(self.owner)
        self.assertEqual(found.device_id, self.profile().device_id)
        self.assertEqual(self.canonical().read_bytes(), race_bytes)
        self.assertEqual(self.legacy().read_bytes(), payload)
        self.assertFalse(list(self.state.glob("*.tmp")))

    def test_same_process_concurrent_creation_generates_once(self):
        with patch.object(profiles, "_create_profile", side_effect=lambda email, **_: self.profile(email)) as create:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(profiles.get_profile, [self.owner, " A.B@EXAMPLE.INVALID "] * 4))
        create.assert_called_once()
        self.assertTrue(all(profile is results[0] for profile in results))
        self.assertEqual(list(profiles._cache), [self.owner])

    def test_symbolic_linked_primary_is_rejected_before_read_or_write(self):
        payload = self.write(self.canonical())
        original = Path.is_symlink
        def symlink(path):
            return path == self.canonical() or original(path)
        with patch.object(Path, "is_symlink", symlink):
            self.assert_private_rejection(lambda: profiles.get_profile(self.owner))
            self.assert_private_rejection(self.profile()._persist)
        self.assertEqual(self.canonical().read_bytes(), payload)

    def test_age_rejects_corrupt_canonical_without_legacy_fallback(self):
        payload = b"fixture-canary"
        self.canonical().write_bytes(payload)
        old = self.write(self.legacy())
        self.assert_private_rejection(lambda: profiles.profile_age_days(self.owner))
        self.assertEqual(self.canonical().read_bytes(), payload)
        self.assertEqual(self.legacy().read_bytes(), old)

    def test_storage_creation_error_uses_fixed_private_diagnostic(self):
        with patch.object(Path, "mkdir", side_effect=OSError("fixture-canary")):
            self.assert_private_rejection(lambda: profiles.get_profile(self.owner))

    def test_invalid_profile_blocks_session_request_before_provider_http(self):
        from moneymin import minute_api
        self.canonical().write_bytes(b"fixture-canary")
        session = minute_api.Session({"email": self.owner, "idToken": "fixture-token"})
        with patch.object(minute_api.Session, "id_token", new_callable=lambda: property(lambda _: "fixture-token")), \
             patch.object(session, "_check_write_policy"), \
             patch.object(minute_api, "_request") as request, \
             patch.object(minute_api, "_request_detailed") as detailed:
            self.assert_private_rejection(lambda: session.request("GET", "/api/v1/orgs"))
            self.assert_private_rejection(lambda: session.request_detailed("GET", "/api/v1/orgs"))
        request.assert_not_called()
        detailed.assert_not_called()

    def test_duplicate_owner_or_nested_keys_preserve_primary_and_block_legacy_fallback(self):
        duplicate_owner = (b'{"email":"a_b@example.invalid","email":"a.b@example.invalid",'
                           b'"device_id":"android.ssaid:fixture","calib":{"distortion_model":"fixture"}}')
        duplicate_nested = json.dumps(self.profile().to_dict()).replace(
            '"distortion_model": "fixture-only"', '"distortion_model":"other","distortion_model":"fixture-only"').encode()
        old = self.write(self.legacy())
        for payload in (duplicate_owner, duplicate_nested):
            with self.subTest(payload=payload):
                self.canonical().write_bytes(payload)
                with patch.object(profiles, "_create_profile") as create:
                    self.assert_private_rejection(lambda: profiles.get_profile(self.owner))
                    self.assert_private_rejection(self.profile()._persist)
                create.assert_not_called()
                self.assertEqual(self.canonical().read_bytes(), payload)
                self.assertEqual(self.legacy().read_bytes(), old)

    def test_duplicate_keys_and_nonfinite_raw_legacy_do_not_publish_or_recreate(self):
        base = json.dumps(self.profile().to_dict())
        for replacement in ('"duplicate":1,"duplicate":2', '"number":NaN',
                            '"number":Infinity', '"number":-Infinity', '"number":1e999'):
            payload = (base[:-1] + "," + replacement + "}").encode()
            with self.subTest(replacement=replacement):
                self.legacy().write_bytes(payload)
                with patch.object(profiles, "_create_profile") as create:
                    self.assert_private_rejection(lambda: profiles.get_profile(self.owner))
                create.assert_not_called()
                self.assertEqual(self.legacy().read_bytes(), payload)
                self.assertFalse(self.canonical().exists())

    def test_exclusive_publisher_checks_exact_raw_payload_with_strict_json(self):
        base = json.dumps(self.profile().to_dict())
        payload = (base[:-1] + ',"ambiguous":1,"ambiguous":2}').encode()
        with patch.object(profiles.os, "link") as link:
            self.assert_private_rejection(lambda: profiles._publish_exclusive(self.canonical(), payload, self.profile()))
        link.assert_not_called()
        self.assertFalse(self.canonical().exists())
        self.assertFalse(list(self.state.glob("*.tmp")))

    def test_nonfinite_or_json_converting_candidate_never_replaces_profile(self):
        before = self.write(self.canonical())
        for value in (float("nan"), float("inf"), -float("inf"), (1, 2), {1: "fixture"}):
            profile = self.profile()
            profile.calib["invalid_fixture"] = value
            with self.subTest(value=type(value).__name__), patch.object(profiles, "save_json") as save:
                self.assert_private_rejection(profile._persist)
                save.assert_not_called()
            self.assertEqual(self.canonical().read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
