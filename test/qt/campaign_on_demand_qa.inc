  static void campaignOnDemandSmoke(MainWindow& window) {
    // This preview never starts a service, campaign or media preparation.
    if (window._contentMode->currentData().toString() != "both"
        || window._contentMode->currentText() != QStringLiteral("Sob demanda")
        || !window._cleanupAfter->isChecked() || window._cacheBudget->value() != 0
        || !window._nymeriaAcquire->text().contains(QStringLiteral("antecipadamente"))
        || window._nymeriaAcquire->isEnabled() || window._cacheStartPending
        || window._nymeriaCommandPending || window._nymeriaRunning) { qApp->exit(180); return; }
    // Restoring an ordinary draft retains the clip duration and the
    // demand acquisition choice. It must not silently enable bulk downloads.
    window._minDuration->setValue(5);
    window._maxDuration->setValue(30);
    window._taskReload.stop();
    window._campaignDraftSave.stop();
    window.saveCampaignDraft();
    MainWindow restored(qobject_cast<oclero::qlementine::QlementineStyle*>(qApp->style()), nullptr, false);
    if (restored._minDuration->value() != 5 || restored._maxDuration->value() != 30
        || !restored._cleanupAfter->isChecked()
        || restored._contentMode->currentData().toString() != "both"
        || restored._cacheBudget->value() != 0 || restored._cacheStartPending
        || restored._nymeriaCommandPending || restored._nymeriaRunning) { qApp->exit(181); return; }
    const auto draft = QJsonDocument::fromJson(QSettings().value("campaign/draft").toByteArray()).object();
    if (draft.contains("target_hours") || !draft.value("run_until_exhausted").toBool()
        || !restored.findChild<QLabel*>("campaignUntilExhausted")) { qApp->exit(181); return; }
    // A stale eight-hour draft cannot make an open campaign require a goal.
    const QJsonObject request{{"run_until_exhausted",true},{"target_hours",8},{"min_dur_s",300},{"max_dur_s",1800}};
    for (const double available : {28800.,316.}) {
      const int count=available>=28800?32:1;
      const QJsonObject account{{"email","fixture@example.invalid"},{"available_seconds",available},
          {"unique_footage_seconds",available},{"unique_footage_upper_bound_seconds",available},
          {"estimated_executable_seconds",available},{"shortfall_seconds",0},
          {"eligible_clips",count},{"estimated_sends",count},{"recorded_clips",0},{"pending_clips",0}};
      QJsonArray clips;
      for (int i=0;i<count;++i) clips.append(QJsonObject{{"clip_uid",QStringLiteral("planned-fixture-%1").arg(i)},
          {"task","Fixture"},{"duration_s",available/count},{"eligible_accounts",QJsonArray{"fixture@example.invalid"}},
          {"excluded_accounts",QJsonArray{}},{"requires_measured_validation",true},{"acquisition_required",true},
          {"capacity_kind","annotation_estimate"},{"readiness","pending_acquisition"}});
      const QJsonObject capacity{{"schema",1},{"known",true},
          {"basis","estimated_admissible_delivery_duration_before_preparation"},{"target_seconds_per_account",0},
          {"can_reach_goal",true},{"available_seconds_min",available},{"available_seconds_max",available},
          {"total_available_seconds",available},{"shortfall_account_count",0},
          {"estimated_sends",count},{"candidate_clips",count},{"accounts",QJsonArray{account}},
          {"pending_acquisition_clips",count},{"requires_measured_validation",true},{"campaign_ready",false}};
      CampaignReviewDialog review({{"ok",true},{"accounts",QJsonObject{{"validated",1}}},
          {"tasks",QJsonObject{{"compatible",1}}},{"clips",count},{"estimated_sends",count},
          {"capacity",capacity},{"clip_plan",clips}}, {"fixture@example.invalid"}, nullptr, request);
      const auto* estimate=review.findChild<QLabel*>("campaignAcquisitionEstimate");
      const auto* table=review.findChild<QTableWidget*>("campaignClipPlanTable");
      const auto* confirm=review.findChild<QPushButton*>("campaignReviewConfirm");
      const auto* summary=review.findChild<QLabel*>("campaignCapacitySummary");
      const auto* accountTable=review.findChild<QTableWidget*>("campaignCapacityTable");
      if (!estimate || !estimate->text().contains(QStringLiteral("dependem dessa validação"))
          || !table || table->item(0,5)->text()!=QStringLiteral("Aquisição e medição pendentes")
          || !confirm || !confirm->isEnabled() || !summary || summary->text().contains("Meta:")
          || !accountTable || !accountTable->isColumnHidden(2)) { qApp->exit(182); return; }
    }
    qApp->exit(0);
  }
