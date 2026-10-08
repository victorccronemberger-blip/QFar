import builtins
import unittest
from unittest.mock import Mock, patch

from moneymin.web import runner
from moneymin import recovery


class CampaignErrorPresentationTests(unittest.TestCase):
    def test_recovery_exposes_cleanup_failure_without_undoing_confirmation(self):
        operation = recovery.RecoveryRunner()
        receipt = {'email': 'fixture@example.invalid', 'session_id': 'fixture-session',
                   'status': 'confirmed', 'index_reconciled': True}
        failure = {'code': 'terminal_archive_preserved', 'session_id': 'old', 'chunk_index': 0}
        with patch.object(recovery, 'resume_account', return_value={
                'items': [receipt], 'reconciled': 1, 'archives_removed': 0,
                'archive_bytes_removed': 0, 'archives_retained': 1,
                'archive_cleanup_errors': [failure]}):
            operation._run(receipt['email'], lambda _email: 'fixture-org', receipt['session_id'])
        state = operation.snapshot()
        self.assertEqual(state['state'], 'pending')
        self.assertIn('Envios confirmados', state['error'])
        self.assertEqual(state['result']['reconciled'], 1)
        self.assertEqual(state['result']['archive_cleanup_errors'], [failure])

    def test_terminal_retry_reports_new_session_and_exact_remaining_attempt(self):
        event = runner._public_event('account_terminal_retry', {
            'email': 'fixture@example.invalid', 'attempt': 3,
            'max_attempts': 5, 'delay_s': 10})
        self.assertEqual(event['stage'], 'Recuperação')
        self.assertIn('encerrou a sessão anterior', event['detail'])
        self.assertIn('Nova sessão', event['detail'])
        self.assertIn('tentativa 3 de 5', event['detail'])
        self.assertNotIn('mesmo envio', event['title'])
        scheduled = runner._public_event('account_retry', {
            'email': 'fixture@example.invalid', 'attempt': 2,
            'max_attempts': 5, 'delay_s': 10})
        self.assertIn('tentativa 2 de 5', scheduled['detail'])

    def test_incomplete_batch_does_not_misclassify_as_video_preparation(self):
        message = runner.friendly_campaign_error(
            "RuntimeError: lote incompleto após todas as tentativas; a campanha "
            "preservou a mídia e não adquiriu outro vídeo. Contas pendentes: "
            "private@example.invalid: Avaliação inconclusiva (HTTP -1)")
        self.assertIn("envio continua sem confirmação", message)
        self.assertNotIn("preparar", message)
        self.assertNotIn("private@example.invalid", message)
        self.assertIn("Recuperação", message)

    def test_evaluation_outage_is_distinct_from_access_and_preparation(self):
        message = runner.friendly_campaign_error("Avaliação inconclusiva (HTTP -1)")
        self.assertIn("avaliação", message)
        self.assertIn("não deve ser removida", message)
        self.assertNotIn("preparar", message)

    def test_terminal_cleanup_error_never_promises_continuation_or_blames_accounts(self):
        message = runner.friendly_campaign_error('Mídia reservada para envio anterior ou limpeza incompleta; a campanha não adquiriu outro vídeo.')
        self.assertIn('campanha parou', message)
        self.assertNotIn('seguirá', message)
        event = runner._public_event('item_incomplete', {'reason': 'media_retained_for_recovery',
            'retained_managed': 3, 'retained_bytes': 4 * 1024 ** 3})
        self.assertEqual(event['stage'], 'Organização')
        self.assertIn('Envios confirmados', event['detail'])
        self.assertIn('4.00 GiB', event['detail'])
        self.assertNotIn('contas não concluíram', event['detail'])
        stopped = runner._public_event('campaign_error', {'error': 'ffmpeg failed private-signed-url'})
        self.assertNotIn('seguirá', stopped['detail'])
        self.assertNotIn('private-signed-url', stopped['detail'])

    def test_real_local_journal_conflict_preserves_recovery_guidance(self):
        event = runner._public_event('account_done', {
            'email': 'fixture@example.invalid', 'ok': False,
            'error': 'O registro de envio já está em uso; preserve a retomada.',
            'session_id': 'private-session', 'campaign_attempts': 1,
        })
        self.assertEqual(event['level'], 'error')
        self.assertIn('operação local', event['detail'])
        self.assertIn('sessão foi preservada', event['detail'])
        self.assertIn('Recuperação de envios', event['detail'])
        self.assertNotIn('Valide a conta', event['detail'])
        self.assertNotIn('private-session', event['detail'])

    def test_unclassified_error_does_not_claim_account_failure_or_retries(self):
        message = runner.friendly_campaign_error(
            'unclassified private-session secret-value https://example.invalid/?token=private')
        self.assertIn('Histórico', message)
        self.assertIn('Recuperação de envios', message)
        for private in ('private-session', 'secret-value', 'token=', 'Valide a conta',
                        'tentativas automáticas'):
            self.assertNotIn(private, message)


class RunnerLifecycleTests(unittest.TestCase):
    def test_failed_thread_creation_or_start_allows_retry(self):
        for kind in (runner.CampaignRunner, runner.BalancesRunner, runner.HoloCacheRunner):
            for failure in ("construct", "start"):
                with self.subTest(runner=kind.__name__, failure=failure):
                    instance = kind()

                    def start():
                        if kind is runner.CampaignRunner:
                            instance.start(runner.CampaignConfig(accounts=[], tasks=[]))
                        elif kind is runner.BalancesRunner:
                            instance.start({}, Mock())
                        else:
                            instance.start(task="test")

                    target = "moneymin.web.runner.threading.Thread"
                    if failure == "start":
                        target += ".start"
                    with patch(target, side_effect=RuntimeError("private diagnostic")):
                        with self.assertRaisesRegex(RuntimeError, "Não foi possível iniciar"):
                            start()
                    self.assertFalse(instance.running)
                    self.assertEqual(instance.state, "error")
                    self.assertEqual(instance.current, "")
                    self.assertIsNone(instance._thread)
                    self.assertNotIn("private diagnostic", str(getattr(instance, "error", "")))
                    with patch("moneymin.web.runner.threading.Thread.start") as thread_start:
                        start()
                    thread_start.assert_called_once()
                    self.assertTrue(instance.running)

    def test_balance_import_failure_releases_running_state(self):
        instance = runner.BalancesRunner()
        instance.state = "running"
        original_import = builtins.__import__

        def import_module(name, *args, **kwargs):
            if name == "crowtado":
                raise ImportError("missing dependency")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_module):
            with self.assertRaises(ImportError):
                instance._run({}, Mock())
        self.assertEqual(instance.state, "error")
        self.assertFalse(instance.running)


class CampaignProgressTests(unittest.TestCase):
    def test_old_skips_failures_and_unfinalized_uploads_do_not_fill_new_goal(self):
        from moneymin.campaign_types import AccountSpec, CampaignConfig
        instance = runner.CampaignRunner()
        cfg = CampaignConfig(accounts=[AccountSpec("a@example.com", "org"),
                                       AccountSpec("b@example.com", "org")], tasks=[],
                             target_hours_per_account=1)
        with patch("moneymin.web.runner.threading.Thread.start"):
            instance.start(cfg)
        for _ in range(100):
            instance._on_event("account_done", {"ok": True, "skipped": True})
        instance._on_event("account_done", {"ok": True, "finalized": False, "credited_seconds": 3600})
        instance._on_event("account_done", {"ok": True, "credited_seconds": 3600})
        instance._on_event("account_done", {"ok": True, "finalized": None, "credited_seconds": 3600})
        self.assertEqual(instance.snapshot()["totals"]["progress_completed"], 0)
        event = {"email": "a@example.com", "task": "task", "clip_uid": "new",
                 "ok": True, "finalized": True, "credited_seconds": 4000}
        instance._on_event("account_done", event)
        instance._on_event("account_done", event)
        totals = instance.snapshot()["totals"]
        self.assertEqual(totals["progress_completed"], 3600)
        self.assertEqual(totals["progress_target"], 7200)
        self.assertEqual(totals["ok_sends"], 1)
        instance.state = "done"
        with patch("moneymin.web.runner.threading.Thread.start"):
            instance.start(cfg)
        self.assertEqual(instance.snapshot()["totals"]["progress_completed"], 0)
