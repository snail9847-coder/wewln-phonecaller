import pytest

from wewln_phonecaller.domain import CampaignStatus
from wewln_phonecaller.storage import Database
from wewln_phonecaller.storage.repository import (
    ConflictError,
    ContactInput,
    InvalidInputError,
    NotionCampaignRecord,
    NotionContactRecord,
    Repository,
)

PHONE_A = "+994501234567"
PHONE_B = "+994551112233"
CAMPAIGN_PAGE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CONTACT_1 = "c1c1c1c1-c1c1-c1c1-c1c1-c1c1c1c1c1c1"
CONTACT_2 = "c2c2c2c2-c2c2-c2c2-c2c2-c2c2c2c2c2c2"


@pytest.fixture
def repo(tmp_path):
    db = Database(tmp_path / "wewln.db")
    db.migrate()
    return Repository(db, default_region="AZ")


def statuses(repo, campaign_id):
    return sorted(
        (j["contact_id"], j["status"], j["reason"]) for j in repo.list_jobs(campaign_id=campaign_id)
    )


def local_campaign(repo, *phones):
    cid = repo.create_local_campaign("Тест")
    rows = [ContactInput(line=i + 2, name=f"Контакт {i}", phone=p) for i, p in enumerate(phones)]
    report = repo.import_contacts(rows, campaign_id=cid)
    return cid, report


def test_local_campaign_lifecycle(repo):
    cid, report = local_campaign(repo, PHONE_A, "050 111 22 33", "garbage")
    assert (report.created, report.added_to_campaign) == (2, 2)
    assert report.rejected == [(4, "invalid")]
    assert repo.list_jobs(campaign_id=cid) == []  # draft: nothing planned
    plan = repo.set_local_campaign_status(cid, CampaignStatus.RUNNING)
    assert plan.created == 2
    assert {j["status"] for j in repo.list_jobs(campaign_id=cid)} == {"queued"}
    # Planning again is idempotent.
    assert repo.plan().created == 0
    assert len(repo.list_jobs(campaign_id=cid)) == 2


def test_import_upserts_by_phone_and_rejects_bad_rows(repo):
    first = repo.import_contacts([ContactInput(2, "Old", PHONE_A)])
    second = repo.import_contacts(
        [ContactInput(2, "New", "+994 50 123 45 67"), ContactInput(3, "x", "")]
    )
    assert (first.created, second.created, second.updated) == (1, 0, 1)
    assert second.rejected == [(3, "missing")]
    contacts = repo.list_contacts()
    assert len(contacts) == 1 and contacts[0]["name"] == "New"


def test_do_not_call_blocks_and_unblocks(repo):
    cid, _ = local_campaign(repo, PHONE_A, PHONE_B)
    repo.set_local_campaign_status(cid, CampaignStatus.RUNNING)
    assert repo.add_do_not_call("050 123 45 67", reason="просил не звонить") is True
    assert repo.add_do_not_call(PHONE_A) is False  # already listed
    jobs = {j["phone"]: (j["status"], j["reason"]) for j in repo.list_jobs(campaign_id=cid)}
    assert jobs[PHONE_A] == ("blocked", "dnc")
    assert jobs[PHONE_B] == ("queued", None)
    removed, remaining = repo.remove_do_not_call(PHONE_A)
    assert (removed, remaining) == (1, [])
    jobs = {j["phone"]: j["status"] for j in repo.list_jobs(campaign_id=cid)}
    assert jobs[PHONE_A] == "queued"
    with pytest.raises(InvalidInputError):
        repo.add_do_not_call("12")


def test_csv_dnc_flag_and_opt_out_survive_manual_removal(repo):
    repo.import_contacts([ContactInput(2, "A", PHONE_A, dnc=True)])
    from wewln_phonecaller.domain import SuppressionSource

    repo.add_do_not_call(PHONE_A, source=SuppressionSource.OPT_OUT, reason="отказ")
    removed, remaining = repo.remove_do_not_call(PHONE_A)
    assert removed == 1 and remaining == ["opt_out"]
    removed, remaining = repo.remove_do_not_call(PHONE_A, all_sources=True)
    assert removed == 1 and remaining == []


def test_stop_is_final_and_cancels_open_jobs(repo):
    cid, _ = local_campaign(repo, PHONE_A, PHONE_B)
    repo.set_local_campaign_status(cid, CampaignStatus.RUNNING)
    repo.stop_campaign(cid, reason="тест")
    jobs = repo.list_jobs(campaign_id=cid)
    assert {(j["status"], j["reason"]) for j in jobs} == {("cancelled", "campaign_stopped")}
    with pytest.raises(ConflictError):
        repo.set_local_campaign_status(cid, CampaignStatus.RUNNING)
    repo.stop_campaign(cid)  # idempotent
    with pytest.raises(ConflictError):
        repo.import_contacts([ContactInput(2, "C", "+994701234567")], campaign_id=cid)


def test_pause_keeps_jobs_but_blocks_new_ones_and_dnc_still_applies(repo):
    cid, _ = local_campaign(repo, PHONE_A)
    repo.set_local_campaign_status(cid, CampaignStatus.RUNNING)
    repo.set_local_campaign_status(cid, CampaignStatus.PAUSED)
    repo.import_contacts([ContactInput(2, "B", PHONE_B)], campaign_id=cid)
    assert [j["phone"] for j in repo.list_jobs(campaign_id=cid)] == [PHONE_A]
    repo.add_do_not_call(PHONE_A)
    assert repo.list_jobs(campaign_id=cid)[0]["status"] == "blocked"
    assert repo.simulate(campaign_id=cid).completed == 0


def test_simulation_rechecks_every_condition(repo):
    cid, _ = local_campaign(repo, PHONE_A, PHONE_B)
    repo.set_local_campaign_status(cid, CampaignStatus.RUNNING)
    report = repo.simulate(campaign_id=cid, limit=1)
    assert report.completed == 1
    repo.add_do_not_call(PHONE_B)
    report = repo.simulate(campaign_id=cid)
    assert report.completed == 0
    jobs = {j["phone"]: (j["status"], j["attempts"]) for j in repo.list_jobs(campaign_id=cid)}
    assert jobs == {PHONE_A: ("done", 1), PHONE_B: ("blocked", 0)}
    with repo.db.connect() as conn:
        results = conn.execute("SELECT outcome, simulated, idempotency_key FROM call_results")
        rows = results.fetchall()
    assert [(r["outcome"], r["simulated"]) for r in rows] == [("simulated", 1)]
    assert rows[0]["idempotency_key"].startswith("wewln:result:local:")


def notion_campaign(status="Запущена", **kw):
    return NotionCampaignRecord(page_id=CAMPAIGN_PAGE, name="Акция", status_label=status, **kw)


def notion_contact(page_id=CONTACT_1, phone=PHONE_A, campaigns=(CAMPAIGN_PAGE,), **kw):
    return NotionContactRecord(
        page_id=page_id, name="Иван", phone=phone, campaign_page_ids=campaigns, **kw
    )


def campaign_id(repo):
    return repo.notion_campaigns()[0]["id"]


def test_notion_campaign_and_contacts_plan_jobs_with_stable_keys(repo):
    repo.apply_notion_campaigns([notion_campaign()], complete=True)
    repo.apply_notion_contacts(
        [notion_contact(), notion_contact(CONTACT_2, phone="bad")], complete=True
    )
    repo.plan()
    jobs = repo.list_jobs(campaign_id=campaign_id(repo))
    assert [(j["status"], j["reason"]) for j in jobs] == [
        ("queued", None),
        ("blocked", "phone_invalid"),
    ]
    assert jobs[0]["idempotency_key"] == "wewln:job:" + "a" * 32 + ":" + "c1" * 16


def test_notion_stop_is_latched_and_restart_is_rejected(repo):
    repo.apply_notion_campaigns([notion_campaign()], complete=True)
    repo.apply_notion_contacts([notion_contact()], complete=True)
    repo.plan()
    repo.apply_notion_campaigns([notion_campaign("Остановлена")], complete=True)
    repo.plan()
    cid = campaign_id(repo)
    assert repo.list_jobs(campaign_id=cid)[0]["status"] == "cancelled"
    repo.apply_notion_campaigns([notion_campaign("Запущена")], complete=True)
    campaign = repo.get_campaign(cid)
    assert campaign["status"] == "stopped"
    assert campaign["notion_push_status"] == 1
    assert "повторный запуск невозможен" in campaign["problem"]


def test_local_stop_of_notion_campaign_wins_and_is_pushed(repo):
    repo.apply_notion_campaigns([notion_campaign()], complete=True)
    cid = campaign_id(repo)
    repo.stop_campaign(cid, reason="жалоба")
    assert [c["id"] for c in repo.campaigns_to_push()] == [cid]
    repo.apply_notion_campaigns([notion_campaign("Запущена")], complete=True)
    campaign = repo.get_campaign(cid)
    assert campaign["status"] == "stopped"
    assert campaign["problem"].startswith("Остановлена в wewln: жалоба")
    repo.mark_campaign_status_pushed(cid, "Остановлена")
    assert repo.campaigns_to_push() == []
    with pytest.raises(ConflictError):
        repo.set_local_campaign_status(cid, CampaignStatus.RUNNING)


def test_unknown_status_language_and_attempts_are_reported(repo):
    repo.apply_notion_campaigns(
        [notion_campaign("Бегом", language="en-US", max_attempts=9)], complete=True
    )
    campaign = repo.get_campaign(campaign_id(repo))
    assert campaign["status"] == "draft"
    assert "Неизвестный статус" in campaign["problem"]
    assert "Язык «en-US»" in campaign["problem"]
    assert campaign["max_attempts"] == 5


def test_removed_and_deleted_contacts_cancel_and_come_back(repo):
    repo.apply_notion_campaigns([notion_campaign()], complete=True)
    repo.apply_notion_contacts([notion_contact()], complete=True)
    repo.plan()
    cid = campaign_id(repo)
    repo.apply_notion_contacts([notion_contact(campaigns=())], complete=True)
    repo.plan()
    assert repo.list_jobs(campaign_id=cid)[0]["reason"] == "contact_removed"
    repo.apply_notion_contacts([notion_contact()], complete=True)
    repo.plan()
    assert repo.list_jobs(campaign_id=cid)[0]["status"] == "queued"
    repo.apply_notion_contacts([], complete=True)  # the page disappeared
    repo.plan()
    assert repo.list_jobs(campaign_id=cid)[0]["reason"] == "contact_deleted"
    repo.apply_notion_contacts([notion_contact()], complete=True)
    repo.plan()
    assert repo.list_jobs(campaign_id=cid)[0]["status"] == "queued"


def test_partial_scan_never_marks_anything_missing(repo):
    repo.apply_notion_campaigns([notion_campaign()], complete=True)
    repo.apply_notion_contacts([notion_contact()], complete=True)
    repo.plan()
    assert repo.apply_notion_contacts([], complete=False)["contacts_missing"] == 0
    assert repo.apply_notion_campaigns([], complete=False)["campaigns_missing"] == 0
    repo.plan()
    assert repo.list_jobs()[0]["status"] == "queued"


def test_missing_campaign_is_paused_not_stopped(repo):
    repo.apply_notion_campaigns([notion_campaign()], complete=True)
    repo.apply_notion_campaigns([], complete=True)
    campaign = repo.get_campaign(campaign_id(repo))
    assert (campaign["status"], campaign["missing_in_notion"]) == ("paused", 1)
    repo.apply_notion_campaigns([notion_campaign()], complete=True)
    assert repo.get_campaign(campaign_id(repo))["status"] == "running"


def test_notion_dnc_checkbox_blocks_and_unchecking_releases(repo):
    repo.apply_notion_campaigns([notion_campaign()], complete=True)
    repo.apply_notion_contacts([notion_contact(dnc=True)], complete=True)
    repo.plan()
    job = repo.list_jobs()[0]
    assert (job["status"], job["reason"]) == ("blocked", "dnc")
    assert repo.list_do_not_call()[0]["source"] == "notion"
    repo.apply_notion_contacts([notion_contact(dnc=False)], complete=True)
    repo.plan()
    assert repo.list_jobs()[0]["status"] == "queued"
    assert repo.list_do_not_call() == []


def test_local_dnc_blocks_notion_contacts_and_is_reported_for_push(repo):
    repo.apply_notion_campaigns([notion_campaign()], complete=True)
    repo.apply_notion_contacts([notion_contact()], complete=True)
    repo.plan()
    repo.add_do_not_call(PHONE_A, reason="оператор")
    assert repo.list_jobs()[0]["status"] == "blocked"
    rows = repo.notion_contacts_with_local_dnc()
    assert [(r["notion_page_id"], r["reason"]) for r in rows] == [(CONTACT_1, "оператор")]
    # Unchecked in Notion again: the local entry keeps the number blocked.
    repo.apply_notion_contacts([notion_contact(dnc=False)], complete=True)
    repo.plan()
    assert repo.list_jobs()[0]["status"] == "blocked"


def test_notion_jobs_need_reconciliation_before_simulation(repo):
    repo.apply_notion_campaigns([notion_campaign()], complete=True)
    repo.apply_notion_contacts([notion_contact()], complete=True)
    repo.plan()
    job = repo.list_jobs()[0]
    assert repo.simulate().completed == 0  # never reconciled with Notion yet
    repo.record_job_page(job["id"], "page-1", {"status": "В очереди"})
    assert repo.simulate().completed == 1


def test_sync_runs_are_recorded_and_pruned(repo):
    for _ in range(5):
        run = repo.start_sync_run()
        repo.finish_sync_run(run, status="ok", stats={"n": 1}, keep=3)
    runs = repo.recent_sync_runs()
    assert len(runs) == 3  # only the newest `keep` runs remain
    assert runs[0]["stats"] == {"n": 1}
