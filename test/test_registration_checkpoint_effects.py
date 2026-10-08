"""A local checkpoint failure must never retry a completed remote creation."""
import pytest

from moneymin import credential_store, config
from moneymin.web import registration_state, server
from test_accounts_creation_e2e import setup, await_terminal, EMAIL, PASSWORD


def test_successful_signup_is_not_repeated_when_its_checkpoint_fails(setup, monkeypatch):
    app, body, remote, minute_register, _ = setup
    save = registration_state.update
    failed = False

    def fail_after_remote_success(email, **kwargs):
        nonlocal failed
        step = kwargs.get('steps', {}).get('crowtado_signup', {})
        if not failed and step.get('status') == 'ok':
            failed = True
            raise OSError('fixture disk full after remote effect')
        return save(email, **kwargs)

    monkeypatch.setattr(registration_state, 'update', fail_after_remote_success)
    client = app.test_client()
    assert client.post('/api/accounts/register?async=1', json=body).status_code == 200
    result = await_terminal(client)
    assert result['state'] == 'failed'
    assert 'armazenamento local' in result['error']
    assert result['created'] == 0
    remote['criar_conta'].assert_called_once()
    minute_register.assert_not_called()
    assert credential_store.lookup(config.SECRETS_DIR, EMAIL, strict=True) == PASSWORD
    checkpoint = registration_state.load()[EMAIL]
    assert checkpoint['steps']['save_partial']['status'] == 'ok'
    assert checkpoint['steps']['crowtado_signup']['remote_effect_possible'] is True


@pytest.mark.parametrize('step_name', ['ban_check', 'minute_register', 'validate'])
def test_later_checkpoint_failure_preserves_previous_remote_steps(setup, monkeypatch, step_name):
    app, body, remote, minute_register, _ = setup
    save = registration_state.update

    def fail_checkpoint(email, **kwargs):
        steps = kwargs.get('steps', {})
        if (steps.get(step_name, {}).get('status') == 'ok'
                and steps.get('crowtado_signup', {}).get('status') == 'ok'
                and (step_name != 'ban_check'
                     or 'Login Crowtado aceito' in steps['ban_check']['detail'])):
            raise OSError('fixture checkpoint storage failure')
        return save(email, **kwargs)

    monkeypatch.setattr(registration_state, 'update', fail_checkpoint)
    client = app.test_client()
    assert client.post('/api/accounts/register?async=1', json=body).status_code == 200
    result = await_terminal(client)
    assert result['state'] == 'failed'
    assert 'armazenamento local' in result['error']
    remote['criar_conta'].assert_called_once()
    remote['login'].assert_called_once()
    assert minute_register.call_count == (0 if step_name == 'ban_check' else 1)
    checkpoint = registration_state.load()[EMAIL]
    assert checkpoint['steps']['crowtado_signup']['status'] == 'ok'
    if step_name == 'ban_check':
        assert 'Login Crowtado aceito' not in checkpoint['steps']['ban_check']['detail']
    else:
        assert step_name not in checkpoint['steps']
    assert credential_store.lookup(config.SECRETS_DIR, EMAIL, strict=True) == PASSWORD


def test_duplicate_login_checkpoint_failure_is_not_reported_as_remote_login_failure(setup, monkeypatch):
    app, body, remote, minute_register, _ = setup
    remote['criar_conta'].side_effect = RuntimeError('EMAIL_EXISTS')
    save = registration_state.update

    def fail_on_duplicate_confirmation(email, **kwargs):
        if kwargs.get('steps', {}).get('crowtado_signup', {}).get('status') == 'skip':
            raise OSError('fixture disk full after duplicate login')
        return save(email, **kwargs)

    monkeypatch.setattr(registration_state, 'update', fail_on_duplicate_confirmation)
    client = app.test_client()
    assert client.post('/api/accounts/register?async=1', json=body).status_code == 200
    result = await_terminal(client)
    assert result['state'] == 'failed'
    assert 'armazenamento local' in result['error']
    remote['criar_conta'].assert_called_once()
    remote['login'].assert_called_once()
    minute_register.assert_not_called()
    checkpoint = registration_state.load()[EMAIL]
    assert checkpoint['steps']['crowtado_signup']['remote_effect_possible'] is True
    assert credential_store.lookup(config.SECRETS_DIR, EMAIL, strict=True) == PASSWORD


def test_batch_stage_storage_failure_stops_before_remote_creation(setup, monkeypatch):
    app, body, remote, minute_register, _ = setup
    save = server._save_registration_batch
    calls = 0

    def fail_after_admission():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('fixture disk full before remote creation')
        return save()

    monkeypatch.setattr(server, '_save_registration_batch', fail_after_admission)
    client = app.test_client()
    assert client.post('/api/accounts/register?async=1', json=body).status_code == 200
    result = await_terminal(client)
    assert result['state'] == 'failed'
    assert 'armazenamento local' in result['error']
    remote['criar_conta'].assert_not_called()
    minute_register.assert_not_called()


def test_synchronous_signup_checkpoint_failure_returns_json_and_saved_steps(setup, monkeypatch):
    app, body, remote, minute_register, _ = setup
    save = registration_state.update

    def fail_after_signup(email, **kwargs):
        if kwargs.get('steps', {}).get('crowtado_signup', {}).get('status') == 'ok':
            raise OSError('FICTIONAL-PRIVATE-STORAGE-DETAIL')
        return save(email, **kwargs)

    monkeypatch.setattr(registration_state, 'update', fail_after_signup)
    response = app.test_client().post('/api/accounts/register', json=body)
    assert response.status_code == 503
    result = response.get_json()
    assert result['ok'] is False and result['partial'] is True
    assert result['code'] == 'local_registration_storage_failure'
    assert result['steps']['save_partial']['status'] == 'ok'
    assert result['steps']['crowtado_signup']['remote_effect_possible'] is True
    assert 'retome o mesmo e-mail' in result['error']
    assert 'FICTIONAL-PRIVATE-STORAGE-DETAIL' not in response.get_data(as_text=True)
    remote['criar_conta'].assert_called_once()
    minute_register.assert_not_called()
