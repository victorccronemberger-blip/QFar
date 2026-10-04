"""Account-route error classification with real readers and temporary fake state."""
from contextlib import ExitStack
import json
import os
from unittest.mock import PropertyMock, patch

import pytest

from moneymin import account_transfer, banned_store, config, minute_api, secure_store, token_store
from moneymin.atomic_io import JsonStateError
from moneymin.web import server


EMAIL = 'fixture@supply.claru.ai'
OTHER = 'other@supply.claru.ai'
CANARY = 'private-fixture-only-canary'
API_SESSION = 'fixture-account-state-local-session'


def token(email=EMAIL):
    return {'email': email, 'localId': 'fixture-uid', 'idToken': 'fixture-token',
            'refreshToken': 'fixture-refresh', 'expires_at': 0}


def state_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob('*')) if p.is_file()}


@pytest.fixture
def local_app(tmp_path):
    data, secrets = tmp_path / 'data', tmp_path / 'secrets'
    data.mkdir(); secrets.mkdir()
    with ExitStack() as stack:
        for name, value in {'ROOT': tmp_path, 'LIBRARY_ROOT': tmp_path, 'DATA_DIR': data,
                            'SECRETS_DIR': secrets, 'INTEGRATIONS_PATH': secrets / 'integrations.dat'}.items():
            stack.enter_context(patch.object(config, name, value))
        for name, value in {'CROWTADO_PW_PATH': secrets / 'crowtado_passwords.json',
                            'PREFS_PATH': data / 'webui_prefs.json',
                            'ACCOUNT_HEALTH_PATH': data / 'account_health.json'}.items():
            stack.enter_context(patch.object(server, name, value))
        stack.enter_context(patch.dict(os.environ, {'QMONEY_LOCAL_API_TOKEN': API_SESSION}))
        trips = []

        def forbidden(*args, **kwargs):
            trips.append('provider-or-dpapi')
            raise AssertionError('Account-state fixtures must never invoke a provider or DPAPI.')

        for obj, name in ((minute_api, '_request'), (minute_api, 'login'), (server, 'login'),
                          (server.Session, 'from_email'), (secure_store, '_crypt'),
                          (secure_store, 'protect_json'), (secure_store, 'unprotect_json')):
            stack.enter_context(patch.object(obj, name, side_effect=forbidden))
        for runner in (server.RUNNER, server.BALANCES_RUNNER, server.ORG_MIGRATION):
            stack.enter_context(patch.object(type(runner), 'running', new_callable=PropertyMock, return_value=False))
        stack.enter_context(patch.dict(server._BULK_REGISTER_STATE, {'state': 'idle'}, clear=True))
        app = server.create_app()  # Real authentication, routes, account locks, and teardown hooks.
        calls = []
        for cls in (JsonStateError, token_store.TokenStoreError, secure_store.SecureStoreError,
                    banned_store.BannedStoreError):
            original = app.error_handler_spec[None][None][cls]

            def delegate(exc, original=original, cls=cls):
                calls.append(cls.__name__)
                return original(exc)

            app.error_handler_spec[None][None][cls] = delegate
        yield {'app': app, 'client': app.test_client(), 'root': tmp_path, 'data': data,
               'secrets': secrets, 'calls': calls, 'tripwires': trips}
        assert trips == []


def post(fixture, route, body=None, *, headers=None):
    if body is None:
        body = {'content': json.dumps([{'email': EMAIL, 'token': token()}]), 'apply': False} if route == 'import' else {}
    if headers is None:
        headers = {'X-QMoney-Session': API_SESSION}
    return fixture['client'].post('/api/accounts/' + route, json=body, headers=headers)


CORRUPT_CASES = [
    ('import', 'passwords', False, JsonStateError), ('import', 'passwords', True, JsonStateError),
    ('import', 'removed', False, JsonStateError), ('import', 'removed', True, JsonStateError),
    ('import', 'token_syntax', False, token_store.TokenStoreError),
    ('import', 'token_syntax', True, token_store.TokenStoreError),
    ('import', 'token_owner', False, token_store.TokenStoreError),
    ('import', 'token_owner', True, token_store.TokenStoreError),
    ('export', 'passwords', False, JsonStateError), ('export', 'preferences', False, JsonStateError),
    ('export', 'removed', False, JsonStateError), ('export', 'jsonl_history', False, JsonStateError),
    ('export', 'new_accounts_history', False, JsonStateError),
    ('export', 'token_syntax', False, token_store.TokenStoreError),
    ('export', 'token_owner', False, token_store.TokenStoreError),
]


@pytest.mark.parametrize('route,kind,apply,error_type', CORRUPT_CASES)
def test_real_local_read_errors_use_specialized_private_response(local_app, route, kind, apply, error_type):
    data, secrets = local_app['data'], local_app['secrets']
    paths = {'passwords': server.CROWTADO_PW_PATH, 'removed': data / 'removed_accounts.json',
             'preferences': server.PREFS_PATH, 'jsonl_history': data / 'contas.jsonl',
             'new_accounts_history': data / 'novas_contas_fixture.json'}
    if kind in paths:
        paths[kind].write_bytes(('{' + CANARY + ':').encode())
    else:
        payload = ('{' + CANARY + ':').encode() if kind == 'token_syntax' else json.dumps(token(OTHER)).encode()
        token_store.record_path(secrets, EMAIL).write_bytes(payload)
    before = state_bytes(local_app['root'])
    body = {'content': json.dumps([{'email': EMAIL, 'token': token()}]), 'apply': apply} if route == 'import' else {}
    response = post(local_app, route, body)
    assert response.status_code == 409
    assert response.json['code'] == ('local_state_unreadable' if error_type is JsonStateError
                                     else 'local_token_identity_conflict')
    assert response.headers['Cache-Control'] == 'no-store'
    assert local_app['calls'] == [error_type.__name__]
    assert CANARY not in response.get_data(as_text=True)
    assert state_bytes(local_app['root']) == before


@pytest.mark.parametrize('error_type,code', [(JsonStateError, 'local_state_unreadable'),
                                           (token_store.TokenStoreError, 'local_token_identity_conflict')])
def test_existing_dispatcher_handlers_do_not_expose_constructed_error_text(local_app, error_type, code):
    # Boundary privacy control: the exception is explicitly constructed, not a reader failure.
    with local_app['app'].test_request_context('/fixture-only-dispatcher'):
        response = local_app['app'].make_response(local_app['app'].handle_user_exception(error_type(CANARY)))
    assert response.status_code == 409
    assert response.json['code'] == code
    assert response.headers['Cache-Control'] == 'no-store'
    assert CANARY not in response.get_data(as_text=True)


@pytest.mark.parametrize('body', [
    {'content': '[]', 'apply': 'yes'}, {'content': []}, [], {'content': '['},
    {'content': '{"email":"a","email":"b"}'},
    {'content': '{"format":"qmoney-accounts","version":2,"accounts":[]}'},
])
def test_invalid_portable_document_and_envelope_remain_input_errors(local_app, body):
    response = post(local_app, 'import', body)
    assert response.status_code == 400
    assert 'code' not in response.json
    assert local_app['calls'] == []
    assert state_bytes(local_app['root']) == {}


@pytest.mark.parametrize('incoming', [token(OTHER), {**token(), 'idToken': 123}])
def test_invalid_incoming_token_remains_a_per_row_input_error(local_app, incoming):
    response = post(local_app, 'import', {'content': json.dumps([{'email': EMAIL, 'token': incoming}]), 'apply': True})
    assert response.status_code == 200
    assert response.json['counts']['invalid'] == 1
    assert response.json['counts']['imported'] == 0
    assert 'code' not in response.json
    assert local_app['calls'] == []
    assert state_bytes(local_app['root']) == {}


@pytest.mark.parametrize('body', [{'emails': 'not-a-list'}, {'emails': []},
                                 {'emails': [EMAIL]}, {'emails': ['not-an-email']}])
def test_invalid_export_selection_remains_an_input_error(local_app, body):
    response = post(local_app, 'export', body)
    assert response.status_code == 400
    assert 'code' not in response.json
    assert local_app['calls'] == []
    assert state_bytes(local_app['root']) == {}


@pytest.mark.parametrize('route', ['import', 'export'])
@pytest.mark.parametrize('error_type,status', [(OSError, 500), (ValueError, 400)])
def test_other_callee_error_categories_are_preserved(local_app, route, error_type, status):
    # Explicit callee injection checks the real route boundary, not the reader.
    message = CANARY if error_type is OSError else 'fixture-portable-input-error'
    with patch.object(account_transfer, route + '_accounts', side_effect=error_type(message)) as injected:
        response = post(local_app, route)
    injected.assert_called_once()
    assert response.status_code == status
    assert 'code' not in response.json
    assert local_app['calls'] == []
    if error_type is OSError:
        assert CANARY not in response.get_data(as_text=True)
    else:
        assert response.json['error'] == message
    assert state_bytes(local_app['root']) == {}


@pytest.mark.parametrize('runner', ['RUNNER', 'BALANCES_RUNNER'])
def test_busy_runner_blocks_apply_before_account_transfer(local_app, runner):
    with patch.object(type(getattr(server, runner)), 'running', new_callable=PropertyMock, return_value=True), \
         patch.object(account_transfer, 'import_accounts', side_effect=AssertionError('busy apply reached transfer')) as downstream:
        response = post(local_app, 'import', {'content': json.dumps([{'email': EMAIL, 'token': token()}]), 'apply': True})
    assert response.status_code == 409
    assert 'code' not in response.json
    downstream.assert_not_called()
    assert state_bytes(local_app['root']) == {}


def test_busy_runner_still_allows_read_only_preview(local_app):
    with patch.object(type(server.RUNNER), 'running', new_callable=PropertyMock, return_value=True):
        response = post(local_app, 'import')
    assert response.status_code == 200
    assert response.json['counts']['new'] == 1
    assert response.json['counts']['imported'] == 0
    assert state_bytes(local_app['root']) == {}


@pytest.mark.parametrize('route', ['import', 'export'])
@pytest.mark.parametrize('headers', [{}, {'X-QMoney-Session': 'wrong-fixture-session'}])
def test_authentication_precedes_real_account_handler(local_app, route, headers):
    with patch.object(account_transfer, 'import_accounts', side_effect=AssertionError('unauthorized import')) as importing, \
         patch.object(server, '_list_accounts', side_effect=AssertionError('unauthorized export')) as listing:
        response = post(local_app, route, headers=headers)
    assert response.status_code == 401
    importing.assert_not_called(); listing.assert_not_called()
    assert local_app['calls'] == []
    assert state_bytes(local_app['root']) == {}


@pytest.mark.parametrize('apply', [False, True])
def test_valid_token_only_import_still_previews_and_applies_without_provider(local_app, apply):
    response = post(local_app, 'import', {'content': json.dumps([{'email': EMAIL, 'token': token()}]), 'apply': apply})
    assert response.status_code == 200
    assert response.json['counts']['imported' if apply else 'new'] == 1
    assert local_app['calls'] == []
    assert not server.CROWTADO_PW_PATH.exists()
    assert not (local_app['secrets'] / 'crowtado_credentials').exists()
    if apply:
        assert token_store.read_file(token_store.record_path(local_app['secrets'], EMAIL), EMAIL) == token()
    else:
        assert state_bytes(local_app['root']) == {}


def test_valid_token_only_export_still_has_no_store(local_app):
    token_store.record_path(local_app['secrets'], EMAIL).write_text(json.dumps(token()), encoding='utf-8')
    before = state_bytes(local_app['root'])
    response = post(local_app, 'export')
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.json['accounts'] == [{'email': EMAIL, 'token': token()}]
    assert local_app['calls'] == []
    assert state_bytes(local_app['root']) == before
