"""Current-batch source reservations, through the real campaign scheduler."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneymin import campaign
from moneymin.campaign_types import AccountSpec, CampaignConfig, TaskSpec


class CampaignBatchFootageTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(campaign.config, "DATA_DIR", self.root))
        self.stack.enter_context(patch.object(campaign, "_ego_clip_inputs", side_effect=lambda clip: (clip, {})))
        self.prepare = self.stack.enter_context(patch.object(campaign, "prepare_clip", side_effect=lambda clip, *a, **kw: {
            "video_path": str(self.root / "fixture.mp4"), "duration_ms": clip["dur_s"]*1000,
            "imu_real": True}))
        for name, value in (("sent_emails", set()), ("is_sent_to_all", False)):
            self.stack.enter_context(patch.object(campaign.sent_registry, name, return_value=value))
        self.mark = self.stack.enter_context(patch.object(campaign.sent_registry, "mark_sent"))
        self.send = self.stack.enter_context(patch.object(campaign, "upload_to_account", side_effect=self.accept))
        self.accounts = [AccountSpec(email, "fixture-org") for email in ("a@example.invalid", "b@example.invalid")]
        self.tasks = [TaskSpec("interior", "Car/scooter washing",180,780, task_name="Cleaning Out Car"),
                      TaskSpec("car", "Car/scooter washing",180,780, task_name="Cleaning Car")]

    @staticmethod
    def accept(item, account, *args, **kwargs):
        return {"email":account.email,"ok":True,"finalized":True}

    def row(self, uid, window, **extra):
        return {"clip_uid":uid,"source":"ego4d","parent_video_uid":"original-parent",
                "window_s":window,"dur_s":window[1]-window[0],**extra}

    def run_fixture(self, first, second, **options):
        cfg = CampaignConfig(self.accounts,self.tasks,work_dir=self.root,
            candidate_plan={"interior":[first],"car":second}, shuffle_schedule=False,
            account_workers=2,cleanup_after_upload=False,**options)
        log = campaign.run_campaign(cfg)
        return log,[(call.args[2],call.args[1].email,call.args[0]["clip_uid"])
                    for call in self.send.call_args_list]

    def test_nearly_identical_source_windows_across_tasks_are_sent_once_per_account(self):
        first = self.row("parent_552.537_743.654",[552.537,743.654])
        variant = self.row("different-encoded-variant",[552.962,743.654])
        log,sends = self.run_fixture(first,[variant])
        self.assertEqual({(task,email) for task,email,_ in sends},
                         {("interior",account.email) for account in self.accounts})
        self.assertEqual(len(sends),2)
        self.assertEqual(self.prepare.call_count,1)
        self.assertEqual(self.mark.call_count,2)
        self.assertEqual(len(log.items),1)

    def test_disjoint_windows_stay_new_even_when_they_share_catalog_aliases(self):
        first = self.row("one",[0,300],dedup_clip_uids=["old-parent-export"])
        disjoint = self.row("two",[300,600],dedup_clip_uids=["old-parent-export"])
        _,sends = self.run_fixture(first,[disjoint])
        self.assertEqual(len(sends),4)
        self.assertEqual(self.prepare.call_count,2)
        self.assertEqual(self.mark.call_count,4)

    def test_small_shared_padding_does_not_discard_new_action(self):
        _,sends = self.run_fixture(self.row("one",[0,300]),[self.row("two",[298,598])])
        self.assertEqual(len(sends),4)
        self.assertEqual(self.prepare.call_count,2)

    def test_account_without_previous_delivery_can_receive_the_overlapping_action(self):
        _,sends = self.run_fixture(self.row("one",[0,300]),[self.row("two",[.5,300.5])],share_clips=False)
        self.assertEqual(sends,[("interior",self.accounts[0].email,"one"),
                               ("car",self.accounts[1].email,"two")])
        self.assertEqual(self.mark.call_count,2)

    def test_known_pre_effect_failure_releases_only_that_accounts_reservation(self):
        def send(item,account,task,*args,**kwargs):
            if task=="interior" and account.email==self.accounts[0].email:
                return {"email":account.email,"ok":False,"retryable":False,"error":"fixture pre-effect failure"}
            return self.accept(item,account)
        self.send.side_effect=send
        _,sends=self.run_fixture(self.row("one",[0,300]),[self.row("two",[.5,300.5])])
        self.assertEqual({(task,email) for task,email,_ in sends},
                         {("interior",a.email) for a in self.accounts}|{("car",self.accounts[0].email)})
        self.assertEqual(len(sends),3)
        self.assertEqual(self.mark.call_count,2)

    def test_pending_remote_result_keeps_source_reserved_across_tasks(self):
        def send(item,account,task,*args,**kwargs):
            if account.email==self.accounts[0].email:
                return {"email":account.email,"ok":False,"session_id":"fixture-pending","uploads":["fixture-upload"]}
            return self.accept(item,account)
        self.send.side_effect=send
        _,sends=self.run_fixture(self.row("one",[0,300]),[self.row("two",[.5,300.5])])
        self.assertEqual(len(sends),2)
        self.assertEqual(self.prepare.call_count,1)
        self.assertEqual(self.mark.call_count,1)

    def test_imu_carve_rechecks_accounts_for_the_effective_disjoint_window(self):
        def prepare(clip,*args,**kwargs):
            if clip["clip_uid"]=="mixed":
                raise ValueError("sem cobertura contínua de IMU")
            return {"video_path":str(self.root/"fixture.mp4"),"duration_ms":300000,"imu_real":True}
        def send(item,account,task,*args,**kwargs):
            if task=="interior" and account.email==self.accounts[1].email:
                return {"email":account.email,"ok":False,"retryable":False,"error":"fixture pre-effect failure"}
            return self.accept(item,account)
        self.prepare.side_effect=prepare
        self.send.side_effect=send
        carved={"clip":self.row("carved-new-action",[300,600]),
                "item":{"video_path":str(self.root/"fixture.mp4"),"duration_ms":300000,"imu_real":True}}
        with patch.object(campaign,"_try_prepare_imu_carve",return_value=carved) as carve:
            _,sends=self.run_fixture(self.row("one",[0,300]),[self.row("mixed",[0,600])])
        self.assertEqual(len(sends),4)
        self.assertEqual({email for task,email,uid in sends if task=="car"},
                         {account.email for account in self.accounts})
        self.assertEqual(self.mark.call_count,3)
        carve.assert_called_once()


if __name__=="__main__":
    unittest.main()
