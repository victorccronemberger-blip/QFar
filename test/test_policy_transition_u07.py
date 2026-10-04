"""U07 local policy transitions and authoritative version-state regression.

Fixtures use temporary roots, artificial clocks/credentials and fake transports.
No provider, real token, real device profile or capture is exercised.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch
from moneymin import appcheck, config, minute_api, vpn

class PolicyRetryTransitionTests(unittest.TestCase):

    def session(self):
        session = minute_api.Session({'idToken': 'artificial-test-value', 'email': 'policy@example.invalid', 'expires_at': 4102444800})
        session._live = True
        return session

    def context(self, stack, state, restriction):
        stack.enter_context(patch.object(vpn, 'ENFORCE', restriction == 'vpn'))
        stack.enter_context(patch.object(vpn, 'vpn_active', side_effect=lambda: state['veto']))
        stack.enter_context(patch.object(config, 'REQUIRE_CURL', False))
        stack.enter_context(patch.object(minute_api, '_version_gate_blocks', side_effect=lambda: state['veto'] if restriction == 'version' else False))
        stack.enter_context(patch.object(minute_api.device_profile, 'get_profile', return_value=SimpleNamespace(headers=lambda **_kw: {})))
        stack.enter_context(patch.object(appcheck, 'get_app_check_header', return_value={}))

    def test_new_veto_blocks_all_later_mutation_attempts(self):
        for detailed in (False, True):
            for restriction in ('version', 'vpn'):
                for veto_before in (1, 2, 3):
                    with self.subTest(detailed=detailed, restriction=restriction, veto_before=veto_before), ExitStack() as stack:
                        state = {'veto': False, 'calls': 0}
                        session = self.session()
                        self.context(stack, state, restriction)

                        def refresh():
                            if veto_before == 1:
                                state['veto'] = True
                        stack.enter_context(patch.object(session, 'refresh', side_effect=refresh))
                        stack.enter_context(patch.object(session, '_relogin'))
                        if veto_before == 1:
                            session._live = False

                        def transport(*_args, **_kw):
                            state['calls'] += 1
                            if state['calls'] == veto_before - 1:
                                state['veto'] = True
                            status = 200 if state['calls'] == veto_before else 401
                            return minute_api.HttpResponse(status, '{}', {}) if detailed else (status, '{}')
                        spy = stack.enter_context(patch.object(minute_api, '_request_detailed' if detailed else '_request', side_effect=transport))
                        if veto_before == 3:
                            # The last 401 already ends the bounded replay.
                            # No subsequent write exists to check the new veto.
                            actual = (session.request_detailed if detailed else session.request)('PATCH', '/api/v1/uploads/fixture-upload/complete', {'size_bytes': 4})
                            self.assertEqual(actual.status if detailed else actual[0], 401)
                            self.assertTrue(state['veto'])
                        else:
                            with self.assertRaises(minute_api.AuthError) as caught:
                                (session.request_detailed if detailed else session.request)('PATCH', '/api/v1/uploads/fixture-upload/complete', {'size_bytes': 4})
                            self.assertEqual(caught.exception.account_issue_code, 'version' if restriction == 'version' else 'policy')
                        self.assertEqual(spy.call_count, veto_before - 1)
                        self.assertFalse(getattr(session, '_refreshing', False))

    def test_read_only_auth_retry_remains_allowed_by_version_latch(self):
        for detailed in (False, True):
            with self.subTest(detailed=detailed), ExitStack() as stack:
                state = {'veto': False, 'calls': 0}
                session = self.session()
                self.context(stack, state, 'version')
                refresh = stack.enter_context(patch.object(session, 'refresh'))

                def transport(*_args, **_kw):
                    state['calls'] += 1
                    state['veto'] = True
                    status = 401 if state['calls'] == 1 else 200
                    return minute_api.HttpResponse(status, '{}', {}) if detailed else (status, '{}')
                spy = stack.enter_context(patch.object(minute_api, '_request_detailed' if detailed else '_request', side_effect=transport))
                response = (session.request_detailed if detailed else session.request)('GET', '/api/v1/users/me')
                self.assertEqual(response.status if detailed else response[0], 200)
                self.assertEqual(spy.call_count, 2)
                refresh.assert_called_once()
                self.assertFalse(session._refreshing)

    def test_app_check_rejection_does_not_refresh_or_relogin(self):
        for detailed in (False, True):
            with self.subTest(detailed=detailed), ExitStack() as stack:
                state = {'veto': False}
                session = self.session()
                self.context(stack, state, 'version')
                stack.enter_context(patch.object(appcheck, 'is_app_check_rejection', return_value=True))
                refresh = stack.enter_context(patch.object(session, 'refresh'))
                relogin = stack.enter_context(patch.object(session, '_relogin'))
                response = minute_api.HttpResponse(401, '{}', {}) if detailed else (401, '{}')
                spy = stack.enter_context(patch.object(minute_api, '_request_detailed' if detailed else '_request', return_value=response))
                actual = (session.request_detailed if detailed else session.request)('PATCH', '/api/v1/uploads/fixture-upload/complete', {})
                self.assertEqual(actual.status if detailed else actual[0], 401)
                spy.assert_called_once()
                refresh.assert_not_called()
                relogin.assert_not_called()

    def test_upload_create_revalidates_org_veto_without_restarting_auth(self):
        for detailed in (False, True):
            with self.subTest(detailed=detailed), ExitStack() as stack:
                state = {'veto': False, 'calls': 0, 'blocked': False}
                session = self.session()
                self.context(stack, state, 'version')
                session.recording_policy = minute_api.RecordingPolicy.parse({'configVersion': 1, 'minDurationMs': 1000, 'maxDurationMs': 10000, 'backlogCapMs': 60000})
                session.device_camera_allowed = True
                warmup = stack.enter_context(patch.object(session, 'warmup'))
                stack.enter_context(patch.object(session, 'org_state', side_effect=lambda _org: {'blocked': state['blocked'], 'userState': 'active', 'cameraSources': ['built-in']}))
                stack.enter_context(patch.object(session, '_check_recording_geo'))
                stack.enter_context(patch.object(minute_api.time, 'time', return_value=datetime(2026, 10, 3, 0, 1, tzinfo=timezone.utc).timestamp()))
                stack.enter_context(patch.object(session, 'refresh'))

                def transport(*_args, **_kw):
                    state['calls'] += 1
                    state['blocked'] = True
                    return minute_api.HttpResponse(401, '{}', {}) if detailed else (401, '{}')
                spy = stack.enter_context(patch.object(minute_api, '_request_detailed' if detailed else '_request', side_effect=transport))
                with self.assertRaises(minute_api.AuthError) as caught:
                    (session.request_detailed if detailed else session.request)('POST', '/api/v1/uploads?org_key=fixture-org', {'duration_ms': 2000, 'recorded_at': '2026-10-03T00:00:55.000Z', 'meta': {'source': 'ego', 'cameras': [{'source': 'builtin'}]}})
                self.assertEqual(caught.exception.account_issue_code, 'restricted')
                spy.assert_called_once()
                self.assertLessEqual(warmup.call_count, 3)
                self.assertFalse(session._refreshing)

    def test_actual_warmup_get_does_not_recurse_into_upload_create(self):
        for detailed in (False, True):
            with self.subTest(detailed=detailed), ExitStack() as stack:
                state = {'veto': False, 'calls': [], 'blocked': False}
                session = self.session()
                self.context(stack, state, 'version')
                warmup = stack.enter_context(patch.object(session, 'warmup', wraps=session.warmup))
                stack.enter_context(patch.object(session, 'camera_model_allowed', return_value=True))
                stack.enter_context(patch.object(minute_api.device_profile, 'get_profile', return_value=SimpleNamespace(headers=lambda **_kw: {}, device_model='fixture')))
                stack.enter_context(patch.object(session, 'org_state', side_effect=lambda _org: {'blocked': state['blocked'], 'userState': 'active', 'cameraSources': ['built-in']}))
                stack.enter_context(patch.object(session, '_check_recording_geo'))
                stack.enter_context(patch.object(minute_api.time, 'time', return_value=datetime(2026, 10, 3, 0, 1, tzinfo=timezone.utc).timestamp()))
                stack.enter_context(patch.object(config, 'PUBLISH_APP_OPENED', False))

                def refresh():
                    state['blocked'] = True
                refresh_spy = stack.enter_context(patch.object(session, 'refresh', side_effect=refresh))
                relogin = stack.enter_context(patch.object(session, '_relogin'))

                def response(_url, method, **_kw):
                    state['calls'].append(method)
                    if method == 'GET':
                        return (200, json.dumps({'configVersion': 1, 'minDurationMs': 1000, 'maxDurationMs': 10000, 'backlogCapMs': 60000}))
                    if method == 'POST':
                        return (401, '{}')
                    raise AssertionError('Unexpected artificial request')
                stack.enter_context(patch.object(minute_api, '_request', side_effect=response))
                stack.enter_context(patch.object(minute_api, '_request_detailed', side_effect=lambda *a, **kw: minute_api.HttpResponse(*response(*a, **kw), {})))
                with self.assertRaises(minute_api.AuthError) as caught:
                    (session.request_detailed if detailed else session.request)('POST', '/api/v1/uploads?org_key=fixture-org', {'duration_ms': 2000, 'recorded_at': '2026-10-03T00:00:55.000Z', 'meta': {'source': 'ego', 'cameras': [{'source': 'builtin'}]}})
                self.assertEqual(caught.exception.account_issue_code, 'restricted')
                self.assertEqual(state['calls'], ['GET', 'POST'])
                self.assertEqual(warmup.call_count, 3)
                refresh_spy.assert_called_once()
                relogin.assert_not_called()
                self.assertFalse(session._refreshing)

    def test_owned_bearer_is_synchronized_after_guard_without_auth_io(self):
        for detailed in (False, True):
            with self.subTest(detailed=detailed), ExitStack() as stack:
                state = {'veto': False, 'guards': 0}
                session = self.session()
                self.context(stack, state, 'version')

                def guard(_method, _path, _body):
                    state['guards'] += 1
                    if state['guards'] == 2:
                        session.data['idToken'] = 'artificial-renewed-value'
                stack.enter_context(patch.object(session, '_check_write_policy', side_effect=guard))
                refresh = stack.enter_context(patch.object(session, 'refresh'))
                relogin = stack.enter_context(patch.object(session, '_relogin'))
                response = minute_api.HttpResponse(200, '{}', {}) if detailed else (200, '{}')
                spy = stack.enter_context(patch.object(minute_api, '_request_detailed' if detailed else '_request', return_value=response))
                (session.request_detailed if detailed else session.request)('PATCH', '/api/v1/uploads/fixture-upload/complete', {})
                self.assertEqual(spy.call_args.kwargs['headers']['Authorization'], 'Bearer artificial-renewed-value')
                refresh.assert_not_called()
                relogin.assert_not_called()

    def test_existing_veto_blocks_preparation_and_auth(self):
        for detailed in (False, True):
            with self.subTest(detailed=detailed), ExitStack() as stack:
                state = {'veto': True}
                session = self.session()
                self.context(stack, state, 'version')
                profile = stack.enter_context(patch.object(minute_api.device_profile, 'get_profile'))
                headers = stack.enter_context(patch.object(appcheck, 'get_app_check_header'))
                refresh = stack.enter_context(patch.object(session, 'refresh'))
                spy = stack.enter_context(patch.object(minute_api, '_request_detailed' if detailed else '_request'))
                with self.assertRaises(minute_api.AuthError) as caught:
                    (session.request_detailed if detailed else session.request)('PATCH', '/api/v1/uploads/fixture-upload/complete', {})
                self.assertEqual(caught.exception.account_issue_code, 'version')
                profile.assert_not_called()
                headers.assert_not_called()
                refresh.assert_not_called()
                spy.assert_not_called()

    def test_second_401_ends_auth_replay_before_third_success(self):
        for detailed in (False, True):
            with self.subTest(detailed=detailed), ExitStack() as stack:
                state = {'veto': False, 'calls': 0}
                session = self.session()
                self.context(stack, state, 'version')
                refresh = stack.enter_context(patch.object(session, 'refresh'))
                relogin = stack.enter_context(patch.object(session, '_relogin'))

                def transport(*_args, **_kw):
                    state['calls'] += 1
                    status = 200 if state['calls'] == 3 else 401
                    return minute_api.HttpResponse(status, '{}', {}) if detailed else (status, '{}')
                spy = stack.enter_context(patch.object(minute_api, '_request_detailed' if detailed else '_request', side_effect=transport))
                actual = (session.request_detailed if detailed else session.request)('PATCH', '/api/v1/uploads/fixture-upload/complete', {})
                self.assertEqual(actual.status if detailed else actual[0], 401)
                self.assertEqual(spy.call_count, 2)
                refresh.assert_called_once()
                relogin.assert_not_called()
                self.assertFalse(session._refreshing)

class AuthoritativeVersionGateTests(unittest.TestCase):

    def test_existing_or_dangling_link_is_not_treated_as_absence(self):
        with patch.object(Path, 'is_symlink', return_value=True), \
                patch.object(minute_api, 'load_json_state') as reader:
            self.veto()
            reader.assert_not_called()


    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='qmoney-version-policy-')))
        self.path = self.root / 'version-gate.json'
        self.stack.enter_context(patch.object(config, 'DATA_DIR', self.root))
        self.stack.enter_context(patch.object(config, 'APP_VERSION', '1.28.0'))
        self.stack.enter_context(patch.object(config, 'REQUIRE_CURL', False))
        self.stack.enter_context(patch.object(vpn, 'ENFORCE', False))
        if hasattr(minute_api, '_VERSION_GATE_MEMORY'):
            self.memory = self.stack.enter_context(patch.object(minute_api, '_VERSION_GATE_MEMORY', {}))
        self.session = minute_api.Session({'idToken': 'artificial-test-value', 'email': 'policy@example.invalid'})

    def veto(self):
        with self.assertRaises(minute_api.AuthError) as caught:
            self.session._check_write_policy('PATCH', '/api/v1/uploads/fixture-upload/complete', {})
        self.assertEqual(caught.exception.account_issue_code, 'version')
        self.assertNotIn(str(self.path), str(caught.exception))
        self.assertTrue(caught.exception.__cause__ is None)

    def observe(self, minimum='99.0.0'):
        minute_api._maybe_latch_version_gate('{"detail":{"error":"app_version_too_old","min_version":"' + minimum + '"}}')

    def test_only_missing_state_is_absence(self):
        self.assertFalse(minute_api._version_gate_blocks())
        self.assertIsNone(self.session.version_gate())
        for raw in (b'{', b'null', b'[]', b'{}', b'{"minVersion":false}', b'{"minVersion":"unknown"}', b'{"minVersion":"99.0.0","minVersion":"0.0.0"}', b'{"minVersion":"99.0.0","latchedAt":NaN}', b'\xff'):
            with self.subTest(shape=raw[:1]):
                self.path.write_bytes(raw)
                self.veto()
                with self.assertRaises(minute_api.AuthError):
                    self.session.version_gate()
                with self.assertRaises(minute_api.AuthError):
                    self.session.clear_version_gate()
                self.assertEqual(self.path.read_bytes(), raw)
                self.session._check_write_policy('GET', '/api/v1/users/me', None)

    def test_unreadable_state_is_private_veto(self):
        original = b'{"minVersion":"99.0.0"}'
        self.path.write_bytes(original)
        with patch.object(Path, 'read_text', side_effect=PermissionError('artificial private diagnostic')):
            self.veto()
            with self.assertRaises(minute_api.AuthError):
                self.session.version_gate()
        self.assertEqual(self.path.read_bytes(), original)

    def test_clear_cannot_bypass_unsatisfied_minimum(self):
        original = b'{"minVersion":"99.0.0"}'
        self.path.write_bytes(original)
        with self.assertRaises(minute_api.AuthError):
            self.session.clear_version_gate()
        self.assertEqual(self.path.read_bytes(), original)
        self.veto()

    def test_explicit_clear_after_update_removes_only_own_root(self):
        self.observe()
        with patch.object(config, 'APP_VERSION', '99.0.0'):
            self.assertFalse(minute_api._version_gate_blocks())
            self.session.clear_version_gate()
        self.assertFalse(self.path.exists())
        self.assertFalse(minute_api._version_gate_blocks())

    def test_failed_atomic_save_retains_memory_veto_and_old_bytes(self):
        original = b'{"minVersion":"1.0.0"}'
        self.path.write_bytes(original)
        with patch.object(minute_api, 'save_json', side_effect=PermissionError('artificial private diagnostic')):
            with patch.object(Path, 'write_text', side_effect=PermissionError('artificial private diagnostic')):
                self.observe()
        self.assertEqual(self.path.read_bytes(), original)
        self.veto()
        self.assertEqual(self.session.version_gate()['minVersion'], '99.0.0')
        self.assertTrue(self.session.version_gate()['persistence_pending'])
        with self.assertRaises(minute_api.AuthError):
            self.session.clear_version_gate()
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_existing_state_is_not_overwritten_by_new_latch(self):
        original = b'{"private-fixture":'
        self.path.write_bytes(original)
        self.observe()
        self.assertEqual(self.path.read_bytes(), original)
        self.veto()

    def test_root_bound_memory_does_not_leak_to_other_installation(self):
        with patch.object(minute_api, 'save_json', side_effect=PermissionError('artificial private diagnostic')):
            with patch.object(Path, 'write_text', side_effect=PermissionError('artificial private diagnostic')):
                self.observe()
        self.veto()
        other = self.root / 'other-root'
        with patch.object(config, 'DATA_DIR', other):
            self.assertFalse(minute_api._version_gate_blocks())
        self.veto()

    def test_known_minimum_never_decreases_on_later_response(self):
        self.observe('99.0.0')
        original = self.path.read_bytes()
        self.observe('2.0.0')
        self.assertEqual(self.session.version_gate()['minVersion'], '99.0.0')
        self.assertEqual(self.path.read_bytes(), original)

    def test_original_403_is_preserved_when_latch_save_fails_both_transports(self):
        for detailed in (False, True):
            with self.subTest(detailed=detailed), ExitStack() as stack:
                self.session._live = True
                self.session.data['expires_at'] = 4102444800
                stack.enter_context(patch.object(self.session, '_check_write_policy'))
                stack.enter_context(patch.object(minute_api.device_profile, 'get_profile', return_value=type('FixtureProfile', (), {'headers': lambda self, **kw: {}})()))
                stack.enter_context(patch.object(appcheck, 'get_app_check_header', return_value={}))
                body = '{"detail":{"error":"app_version_too_old","min_version":"99.0.0"}}'
                response = minute_api.HttpResponse(403, body, {}) if detailed else (403, body)
                stack.enter_context(patch.object(minute_api, '_request_detailed' if detailed else '_request', return_value=response))
                stack.enter_context(patch.object(minute_api, 'save_json', side_effect=PermissionError('artificial private diagnostic')))
                stack.enter_context(patch.object(Path, 'write_text', side_effect=PermissionError('artificial private diagnostic')))
                actual = (self.session.request_detailed if detailed else self.session.request)('PATCH', '/api/v1/uploads/fixture-upload/complete', {})
                self.assertEqual(actual, response)
                self.assertTrue(minute_api._version_gate_blocks())

    def test_invalid_candidate_metadata_preserves_disk_and_pending_veto(self):
        original = b'{"minVersion":"1.0.0","appVersion":"1.28.0","latchedAt":123}'
        for version, clock in (('invalid-client-version', 123), (None, 123), (False, 123), ('1.28.0', -1)):
            with self.subTest(version=type(version).__name__, clock=clock):
                self.memory.clear()
                self.path.write_bytes(original)
                with patch.object(config, 'APP_VERSION', version), patch.object(minute_api.time, 'time', return_value=clock), patch.object(minute_api, 'save_json') as writer:
                    self.observe()
                writer.assert_not_called()
                self.assertEqual(self.path.read_bytes(), original)
                state = self.session.version_gate()
                self.assertEqual(state['minVersion'], '99.0.0')
                self.assertTrue(state['persistence_pending'])
                self.veto()
                with self.assertRaises(minute_api.AuthError):
                    self.session.clear_version_gate()
                self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_current_version_preserves_original_403_both_transports(self):
        original = b'{"minVersion":"1.0.0","appVersion":"1.28.0","latchedAt":123}'
        for detailed in (False, True):
            with self.subTest(detailed=detailed), ExitStack() as stack:
                self.memory.clear()
                self.path.write_bytes(original)
                self.session._live = True
                self.session.data['expires_at'] = 4102444800
                stack.enter_context(patch.object(self.session, '_check_write_policy'))
                stack.enter_context(patch.object(minute_api.device_profile, 'get_profile', return_value=type('FixtureProfile', (), {'headers': lambda self, **kw: {}})()))
                stack.enter_context(patch.object(appcheck, 'get_app_check_header', return_value={}))
                stack.enter_context(patch.object(config, 'APP_VERSION', 'invalid-client-version'))
                body = '{"detail":{"error":"app_version_too_old","min_version":"99.0.0"}}'
                response = minute_api.HttpResponse(403, body, {}) if detailed else (403, body)
                stack.enter_context(patch.object(minute_api, '_request_detailed' if detailed else '_request', return_value=response))
                actual = (self.session.request_detailed if detailed else self.session.request)('PATCH', '/api/v1/uploads/fixture-upload/complete', {})
                self.assertEqual(actual, response)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(self.session.version_gate()['minVersion'], '99.0.0')
                self.assertTrue(self.session.version_gate()['persistence_pending'])
            self.veto()
