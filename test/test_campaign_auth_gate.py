import unittest
from unittest.mock import Mock, patch

from moneymin import campaign
from moneymin.campaign_types import AccountSpec
from moneymin.minute_api import AuthError


class CampaignAuthGateTests(unittest.TestCase):
    def test_ego4d_without_confirmed_real_sensors_never_opens_session(self):
        for flag in (None, False, 'true', 1):
            with self.subTest(flag=flag), patch.object(campaign.Session, 'from_email') as session, \
                 patch.object(campaign, 'upload_session') as upload:
                result = campaign.upload_to_account({'source': 'ego4d', 'imu_real': flag},
                    AccountSpec('fixture@example.invalid', 'org'), 'task', 30, True, True)
                self.assertFalse(result['ok'])
                self.assertFalse(result['finalized'])
                self.assertFalse(result['retryable'])
                session.assert_not_called()
                upload.assert_not_called()

    def test_missing_ego4d_csv_cannot_fall_back_to_synthetic_sensor_data(self):
        session = Mock(_live=True)
        session.recording_policy.limits.return_value = {'min_duration_ms': 60000, 'max_duration_ms': 1800000}
        item = {'source': 'ego4d', 'imu_real': True, 'duration_ms': 60000,
                'video_path': 'fixture-only.mp4', 'clip_uid': 'fixture',
                'probe': {'duration_ms': 60000, 'fps': 30}}
        with patch.object(campaign.org_policy, 'account_kind', return_value='other'), \
             patch.object(campaign.device_profile, 'get_profile', return_value=Mock()), \
             patch.object(campaign, 'build_imu_csv') as synthetic, \
             patch.object(campaign, 'upload_session') as upload:
            result = campaign.upload_to_account(item, AccountSpec('fixture@example.invalid', 'org'),
                'task', 30, True, True, session=session, recover_pending=False)
        self.assertFalse(result['ok'])
        self.assertFalse(result['retryable'])
        self.assertIn('sem substituição sintética', result['error'])
        synthetic.assert_not_called()
        upload.assert_not_called()

    def test_refreshed_firebase_token_does_not_skip_minute_access_validation(self):
        session = Mock(_live=True)
        session.ensure_auth.side_effect = AuthError("Conta desativada", code="restricted")
        cache = {}
        account = AccountSpec("account@example.invalid", "org")
        with patch.object(campaign.Session, "from_email", return_value=session), \
             patch.object(campaign.org_policy, "account_kind", return_value="other"), \
             patch.object(campaign, "upload_session") as upload:
            result = campaign.upload_to_account({}, account, "task", 30, True, True,
                                                session_cache=cache)
        session.ensure_auth.assert_called_once_with(org_key="org")
        upload.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertTrue(result["restriction_confirmed"])
        self.assertFalse(result["retryable"])
        self.assertEqual(cache, {})

    def test_temporary_access_failure_preserves_cause_without_caching_session(self):
        session = Mock(_live=True)
        session.ensure_auth.side_effect = AuthError("Consulta indisponível", code="service")
        cache = {}
        with patch.object(campaign.Session, "from_email", return_value=session), \
             patch.object(campaign.org_policy, "account_kind", return_value="other"):
            result = campaign.upload_to_account({}, AccountSpec("a@example.invalid", "org"),
                                                "task", 30, True, True, session_cache=cache)
        self.assertTrue(result["retryable"])
        self.assertFalse(result["restriction_confirmed"])
        self.assertEqual(cache, {})
