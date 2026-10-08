"""Registration recovery never duplicates a possibly accepted remote POST."""
import time
from unittest.mock import Mock

import pytest
from moneymin import config, credential_store, minute_api
from moneymin.web import registration_state, server, account_health
from test_accounts_creation_e2e import setup, await_terminal, EMAIL, PASSWORD


def submit(client, body):
    assert client.post('/api/accounts/register?async=1', json=body).status_code == 200
    return await_terminal(client)


def test_ambiguous_crowtado_failure_recovery_uses_login_and_never_signup(setup):
    app, body, remote, register, _ = setup
    error = server.crowtado.CrowtadoError('response lost', code='timeout',
        phase='signup_submission', remote_effect_possible=True)
    remote['criar_conta'].side_effect = error
    client = app.test_client()
    assert submit(client, body)['state'] == 'failed'
    saved = registration_state.load()[EMAIL]['steps']['crowtado_signup']
    assert saved['remote_effect_possible'] is True
    assert saved['phase'] == 'signup_submission'
    assert client.post(f'/api/accounts/{EMAIL}/resume', json={}).status_code == 200
    assert await_terminal(client)['created'] == 1
    remote['criar_conta'].assert_called_once()
    remote['login'].assert_called_once()


def test_proven_pre_submit_failure_can_retry_once(setup):
    app, body, remote, register, _ = setup
    remote['criar_conta'].side_effect = [server.crowtado.CrowtadoError(
        'browser failed before submit', phase='browser_setup', remote_effect_possible=False), None]
    assert submit(app.test_client(), body)['created'] == 1
    assert remote['criar_conta'].call_count == 2


def test_existing_account_failed_login_keeps_login_only_recovery(setup):
    app, body, remote, register, _ = setup
    remote['criar_conta'].side_effect = server.crowtado.CrowtadoError(
        'Esta conta já existe no Crowtado.', code='account_exists',
        provider_error_code='form_identifier_exists')
    remote['login'].side_effect = server.crowtado.CrowtadoError('response lost', code='network')
    client = app.test_client()
    assert submit(client, body)['failed'] == 1
    assert registration_state.load()[EMAIL]['steps']['crowtado_signup']['remote_effect_possible'] is True
    remote['login'].side_effect = None
    assert client.post(f'/api/accounts/{EMAIL}/resume', json={}).status_code == 200
    assert await_terminal(client)['created'] == 1
    remote['criar_conta'].assert_called_once()


def test_huge_lockout_duration_stays_locked_without_invalid_cooldown():
    error = server.crowtado._remote_error('Login', 403, {'errors':[
        {'code':'user_locked','meta':{'lockout_expires_in_seconds':10**1000}}]})
    assert error.account_issue_code == 'account_locked'
    assert error.retry_after_seconds is None


def test_confirmed_minute_post_failed_login_resumes_without_new_post(setup, monkeypatch):
    app, body, remote, register, _ = setup
    login = Mock(side_effect=minute_api.AuthError('login response lost', code='timeout'))
    monkeypatch.setattr(minute_api, 'login', login)
    client = app.test_client()
    assert submit(client, body)['failed'] == 1
    assert registration_state.load()[EMAIL]['steps']['minute_identity']['status'] == 'ok'
    login.side_effect = None
    assert client.post(f'/api/accounts/{EMAIL}/resume', json={}).status_code == 200
    assert await_terminal(client)['created'] == 1
    register.assert_called_once()
    remote['criar_conta'].assert_called_once()


def test_unknown_minute_post_failure_only_verifies_access_on_resume(setup):
    app, body, remote, register, _ = setup
    register.side_effect = RuntimeError('ambiguous response')
    client = app.test_client()
    assert submit(client, body)['state'] == 'failed'
    assert registration_state.load()[EMAIL]['steps']['minute_identity']['remote_effect_possible']
    assert client.post(f'/api/accounts/{EMAIL}/resume', json={}).status_code == 200
    assert await_terminal(client)['created'] == 1
    register.assert_called_once()


@pytest.mark.parametrize('code', ['rate_limit', 'account_locked', 'restricted'])
def test_provider_stop_does_not_start_second_identity(setup, monkeypatch, code):
    app, body, remote, register, _ = setup
    identities = Mock(side_effect=[dict(body, senha=PASSWORD, nome='QA', sobrenome='Fixture'),
        dict(body, email='second@example.invalid', senha=PASSWORD, nome='QA', sobrenome='Second')])
    monkeypatch.setattr(server.identity, 'gerar_identidade', identities)
    error = server.crowtado.CrowtadoError('provider refused', code=code,
        retry_after_seconds=1800.1 if code != 'restricted' else None,
        remote_effect_possible=True, phase='signup_submission')
    remote['criar_conta'].side_effect = error
    client = app.test_client()
    assert client.post('/api/accounts/bulk-register', json={'domain':'example.invalid', 'count':2}).status_code == 200
    result = await_terminal(client)
    assert result['state'] == 'failed' and result['completed'] == 1 and result['total'] == 2
    identities.assert_called_once()
    remote['criar_conta'].assert_called_once()
    register.assert_not_called()
    if code != 'restricted':
        step = result['results'][0]['steps']['crowtado_signup']
        assert step['retry_after_seconds'] == 1801
        assert step['retry_at'] > time.time() + 1700


def test_user_locked_preserves_credentials_and_never_archives(setup):
    app, body, remote, register, _ = setup
    remote['login'].side_effect = server.crowtado._remote_error('Login', 403,
        {'errors':[{'code':'user_locked', 'meta':{'lockout_expires_in_seconds':1800}}]})
    client = app.test_client()
    result = submit(client, body)
    assert result['results'][0]['steps']['ban_check']['code'] == 'account_locked'
    assert client.get('/api/accounts/banned').get_json()['accounts'] == []
    assert len(client.get('/api/accounts').get_json()['accounts']) == 1
    assert credential_store.lookup(config.SECRETS_DIR, EMAIL, strict=True) == PASSWORD
    assert account_health.confirmed_ban(EMAIL, {}, registration_state.load()[EMAIL]) is None
    assert client.post(f'/api/accounts/{EMAIL}/resume', json={}).status_code == 200
    assert await_terminal(client)['created'] == 0
    remote['login'].assert_called_once()  # no repeat before provider deadline
    remote['criar_conta'].assert_called_once()


def test_confirmed_ban_keeps_sanitized_provider_evidence_after_checkpoint_purge(setup):
    app, body, remote, register, _ = setup
    remote['login'].side_effect = server.crowtado._remote_error('Login', 403,
        {'errors':[{'code':'user_banned', 'long_message':'secret-body-fixture'}]})
    client = app.test_client()
    assert submit(client, body)['failed'] == 1
    assert client.get('/api/accounts').get_json()['accounts'] == []
    archived = client.get('/api/accounts/banned').get_json()['accounts'][0]
    assert archived['diagnostic']['provider_error_code'] == 'user_banned'
    assert archived['diagnostic']['provider'] == 'crowtado'
    assert archived['diagnostic']['http_status'] == 403
    assert archived['diagnostic']['first_observed_at']
    assert 'secret-body-fixture' not in str(archived)


def test_synchronous_cooldown_does_not_change_completed_checkpoint(setup, monkeypatch):
    app, body, remote, register, _ = setup
    client = app.test_client()
    assert submit(client, body)['created'] == 1
    before = (config.DATA_DIR / 'account_registrations.json').read_bytes()
    server.registration_limits.defer('crowtado', 1800)
    assign = Mock(side_effect=AssertionError('cooldown must not assign or check proxy'))
    from moneymin import registration_proxy
    monkeypatch.setattr(registration_proxy, 'assign', assign)
    response = client.post('/api/accounts/register', json=body)
    assert response.status_code == 429 and response.get_json()['not_admitted'] is True
    assert (config.DATA_DIR / 'account_registrations.json').read_bytes() == before
    remote['criar_conta'].assert_called_once()
    register.assert_called_once()
    assign.assert_not_called()


def test_rate_limit_storage_failure_preserves_deadline_checkpoint_and_blocks_new_identity(setup, monkeypatch):
    app, body, remote, register, _ = setup
    monkeypatch.setattr(server.registration_limits, 'save_json', Mock(side_effect=OSError('fixture write failure')))
    remote['criar_conta'].side_effect = server.crowtado.CrowtadoError('limit', code='rate_limit', retry_after_seconds=1800)
    client = app.test_client()
    result = submit(client, body)
    assert result['failed'] == 1
    step = registration_state.load()[EMAIL]['steps']['crowtado_signup']
    assert step['code'] == 'rate_limit' and step['retry_after_seconds'] == 1800
    assert step['retry_at'] > time.time() + 1700
    response = client.post('/api/accounts/register?async=1', json={**body,'email':'second@example.invalid'})
    assert response.status_code == 429
    remote['criar_conta'].assert_called_once()


def test_rate_limit_blocks_new_identity_across_service_app_restart(setup):
    app, body, remote, register, _ = setup
    remote['criar_conta'].side_effect = server.crowtado.CrowtadoError(
        'rate limited', code='rate_limit', retry_after_seconds=1800,
        remote_effect_possible=False)
    client = app.test_client()
    assert submit(client, body)['failed'] == 1
    # Recreate the Flask app; the deadline lives on disk, not in this instance.
    restarted = server.create_app(for_testing=True).test_client()
    response = restarted.post('/api/accounts/register?async=1',
        json={**body, 'email':'next@example.invalid'})
    assert response.status_code == 429
    assert response.get_json()['not_admitted'] is True
    assert response.get_json()['retry_after_seconds'] > 1700
    remote['criar_conta'].assert_called_once()
    assert not credential_store.record_path(config.SECRETS_DIR, 'next@example.invalid').exists()
    with pytest.MonkeyPatch.context() as patch:
        checks = Mock(side_effect=AssertionError('cooldown must not access provider'))
        patch.setattr(server, '_preflight_checks', checks)
        preflight = restarted.get('/api/accounts/bulk-register/preflight?domain=example.invalid').get_json()
        assert preflight['ready'] is False and preflight['retry_after_seconds'] > 1700
        checks.assert_not_called()
