"""The Notion sync against the mock API: idempotency, stop, do-not-call, failures, privacy."""

import json
import logging
from datetime import UTC, datetime

from wewln_phonecaller.notion.mock_server import stepping_clock
from wewln_phonecaller.notion.setup import resolve_workspace
from wewln_phonecaller.notion.sync import LOCK_NAME, SyncService, campaign_summary

PHONE_A = "+994501234567"
PHONE_B = "+994551112233"


def by_title(rows, column="Задание"):
    return {row[column].split(" — ")[0]: row for row in rows}


async def test_first_sync_plans_jobs_and_a_repeated_sync_writes_nothing(notion_world):
    w = notion_world
    camp = w.campaign()
    w.contact("Анна", PHONE_A, [camp])
    w.contact("Борис", "0551112233", [camp], dnc=True)
    vera = w.contact("Вера", "12", [camp])

    first = await w.run()
    assert first.status == "ok", first
    assert first.stats["jobs_created"] == 3
    jobs = by_title(w.rows("jobs"))
    assert jobs["Анна"]["Статус"] == "В очереди"
    assert jobs["Борис"]["Статус"] == "Заблокировано"
    assert jobs["Борис"]["Причина"] == "Номер в списке «Не звонить»"
    assert jobs["Вера"]["Причина"] == "Номер телефона не распознан"
    assert jobs["Анна"]["Ключ"].startswith("wewln:job:")
    assert w.row(vera)["Ошибка"] == "Номер телефона не распознан"
    assert w.row(camp)["Сводка"].startswith("Заданий: 3")

    writes = w.writes()
    for _ in range(2):
        again = await w.run()
        assert again.status == "ok"
        assert "jobs_created" not in again.stats
    assert w.writes() == writes  # nothing changed, nothing written
    assert len(w.rows("jobs")) == 3
    assert len(w.repo.list_jobs()) == 3


async def test_simulated_result_is_pushed_once(notion_world):
    w = notion_world
    camp = w.campaign()
    w.contact("Анна", PHONE_A, [camp])
    await w.run()
    assert w.repo.simulate(limit=10).completed == 1

    result = await w.run()
    assert result.stats["results_created"] == 1
    (job,) = w.rows("jobs")
    assert job["Статус"] == "Выполнено"
    assert job["Попытки"] == 1
    (row,) = w.rows("results")
    assert row["Исход"] == "Симуляция"
    assert row["Симуляция"] is True
    assert row["Ключ"].endswith(":1")
    assert row["Задание"] == [w.pages("jobs")[0]["id"]]

    await w.run()
    assert len(w.rows("results")) == 1
    assert w.repo.simulate(limit=10).completed == 0


async def test_lost_local_database_reuses_pages_and_never_calls_twice(notion_world):
    w = notion_world
    camp = w.campaign()
    w.contact("Анна", PHONE_A, [camp])
    w.contact("Борис", PHONE_B, [camp])
    await w.run()
    assert w.repo.simulate(limit=1).completed == 1
    await w.run()
    assert (len(w.rows("jobs")), len(w.rows("results"))) == (2, 1)

    restored = w.new_repo(w.tmp_path / "new.db")  # e.g. the data folder was deleted
    sync = w.new_sync(restored)
    result = await w.run(sync)
    assert result.status == "ok", result
    assert result.stats["jobs_adopted"] == 2
    assert result.stats["jobs_state_adopted"] == 1
    assert "jobs_created" not in result.stats
    assert sorted(j["status"] for j in restored.list_jobs()) == ["done", "queued"]

    assert restored.simulate(limit=10).completed == 1  # only the contact not called yet
    await w.run(sync)
    assert (len(w.rows("jobs")), len(w.rows("results"))) == (2, 2)
    keys = [row["Ключ"] for row in w.rows("results")]
    assert len(set(keys)) == 2


async def test_stop_in_notion_is_final_and_a_restart_is_reverted(notion_world):
    w = notion_world
    camp = w.campaign()
    w.contact("Анна", PHONE_A, [camp])
    await w.run()

    w.mock.update_row(camp, {"Статус": "Остановлена"})
    await w.run()
    (job,) = w.rows("jobs")
    assert (job["Статус"], job["Причина"]) == ("Отменено", "Кампания остановлена")
    assert w.repo.list_jobs()[0]["status"] == "cancelled"

    w.mock.update_row(camp, {"Статус": "Запущена"})
    result = await w.run()
    assert result.stats["campaign_statuses_pushed"] == 1
    assert w.row(camp)["Статус"] == "Остановлена"
    assert "повторный запуск невозможен" in w.row(camp)["Ошибка"]
    assert w.repo.simulate(limit=10).completed == 0
    await w.run()
    assert w.row(camp)["Ошибка"] == ""  # the status is consistent again


async def test_local_stop_works_while_notion_is_down_and_reaches_notion_later(notion_world):
    w = notion_world
    camp = w.campaign()
    w.contact("Анна", PHONE_A, [camp])
    await w.run()
    campaign_id = w.repo.notion_campaigns()[0]["id"]

    w.mock.down = True
    w.repo.stop_campaign(campaign_id, reason="Жалоба клиента")
    failed = await w.run()
    assert failed.status == "failed"
    assert failed.error_code == "notion_unavailable"
    assert w.repo.list_jobs()[0]["status"] == "cancelled"
    assert w.repo.simulate(limit=10).completed == 0

    w.mock.down = False
    result = await w.run()
    assert result.status == "ok", result
    assert w.row(camp)["Статус"] == "Остановлена"
    await w.run()
    assert w.row(camp)["Ошибка"] == "Остановлена в wewln: Жалоба клиента"
    assert w.rows("jobs")[0]["Статус"] == "Отменено"


async def test_do_not_call_works_in_both_directions(notion_world):
    w = notion_world
    camp = w.campaign()
    anna = w.contact("Анна", PHONE_A, [camp])
    boris = w.contact("Борис", PHONE_B, [camp])
    await w.run()

    w.mock.update_row(anna, {"Не звонить": True, "Причина запрета": "Просила не звонить"})
    w.repo.add_do_not_call(PHONE_B, reason="Отказ по телефону")
    result = await w.run()
    assert result.stats["contacts_dnc_pushed"] == 1
    assert w.row(boris)["Не звонить"] is True
    assert w.row(boris)["Причина запрета"] == "Отказ по телефону"
    assert w.row(anna)["Причина запрета"] == "Просила не звонить"
    jobs = by_title(w.rows("jobs"))
    assert {jobs[n]["Статус"] for n in ("Анна", "Борис")} == {"Заблокировано"}
    assert w.repo.simulate(limit=10).completed == 0

    w.mock.update_row(anna, {"Не звонить": False})
    await w.run()
    jobs = by_title(w.rows("jobs"))
    assert jobs["Анна"]["Статус"] == "В очереди"
    assert jobs["Борис"]["Статус"] == "Заблокировано"


async def test_cancel_in_notion_is_respected(notion_world):
    w = notion_world
    camp = w.campaign()
    w.contact("Анна", PHONE_A, [camp])
    await w.run()
    (page,) = w.pages("jobs")

    w.mock.update_row(page["id"], {"Статус": "Отменено"})
    result = await w.run()
    assert result.stats["jobs_cancelled_in_notion"] == 1
    job = w.repo.list_jobs()[0]
    assert (job["status"], job["reason"]) == ("cancelled", "cancelled_in_notion")
    assert w.row(page["id"])["Причина"] == "Отменено вручную в Notion"
    assert w.repo.simulate(limit=10).completed == 0


async def test_other_manual_status_edits_are_reverted(notion_world):
    w = notion_world
    camp = w.campaign()
    w.contact("Анна", PHONE_A, [camp])
    await w.run()
    (page,) = w.pages("jobs")
    w.mock.update_row(page["id"], {"Статус": "Выполнено"})
    result = await w.run()
    assert result.stats["jobs_updated"] == 1
    assert w.row(page["id"])["Статус"] == "В очереди"
    assert w.repo.list_jobs()[0]["status"] == "queued"


async def test_deleted_job_page_cancels_the_job_and_is_not_recreated(notion_world):
    w = notion_world
    camp = w.campaign()
    w.contact("Анна", PHONE_A, [camp])
    await w.run()
    (page,) = w.pages("jobs")

    w.mock.trash(page["id"])
    result = await w.run()
    assert result.stats["jobs_cancelled_page_deleted"] == 1
    job = w.repo.list_jobs()[0]
    assert (job["status"], job["reason"]) == ("cancelled", "page_deleted_in_notion")
    for _ in range(2):
        await w.run()
    assert w.pages("jobs") == []
    assert w.mock.count("POST", r"^/v1/pages$") == 1


async def test_removed_contact_is_cancelled_and_comes_back(notion_world):
    w = notion_world
    camp = w.campaign()
    anna = w.contact("Анна", PHONE_A, [camp])
    await w.run()

    w.mock.update_row(anna, {"Кампании": []})
    await w.run()
    (job,) = w.rows("jobs")
    assert (job["Статус"], job["Причина"]) == ("Отменено", "Контакт убран из кампании")

    w.mock.update_row(anna, {"Кампании": [camp]})
    await w.run()
    (job,) = w.rows("jobs")
    assert (job["Статус"], job["Причина"]) == ("В очереди", "")


async def test_deleted_campaign_page_pauses_the_campaign(notion_world):
    w = notion_world
    camp = w.campaign()
    w.contact("Анна", PHONE_A, [camp])
    await w.run()
    w.mock.trash(camp)
    await w.run()
    campaign = w.repo.notion_campaigns()[0]
    assert (campaign["status"], campaign["missing_in_notion"]) == ("paused", 1)
    assert w.repo.simulate(limit=10).completed == 0

    w.mock.trash(camp, in_trash=False)
    await w.run()
    assert w.repo.notion_campaigns()[0]["status"] == "running"


async def test_rate_limit_is_waited_for(notion_world, sleeps):
    w = notion_world
    w.campaign()
    w.mock.fail("POST", r"/query$", 429, "rate_limited", times=2, retry_after=3)
    result = await w.run()
    assert result.status == "ok", result
    assert sum(1 for s in sleeps if s >= 3) >= 2


async def test_long_retry_after_ends_the_cycle_and_the_service_backs_off(notion_world):
    w = notion_world
    w.mock.fail("GET", r"^/v1/databases/", 429, "rate_limited", retry_after=120)
    result = await w.run()
    assert (result.status, result.error_code, result.retry_after) == (
        "failed",
        "notion_rate_limited",
        120,
    )
    service = SyncService(w.sync, interval=60)
    assert service.delay_after(result) >= 120
    assert service.status()["status"] == "degraded"
    assert (await w.run()).status == "ok"
    assert service.status()["status"] == "ok"
    assert service.delay_after(await w.run()) == 60


async def test_blocked_requests_are_not_retried(notion_world):
    w = notion_world
    w.mock.requests.clear()
    w.mock.fail(
        "GET",
        r"^/v1/databases/",
        429,
        "rate_limited",
        rate_limit_reason="public_api_request_blocked",
    )
    result = await w.run()
    assert result.status == "failed"
    assert len(w.mock.requests) == 1


async def test_backoff_grows_and_is_capped():
    class FakeSync:
        consecutive_failures = 0

    from wewln_phonecaller.notion.sync import SyncResult

    sync = FakeSync()
    service = SyncService(sync, interval=60, max_backoff=900)
    failed = SyncResult("failed", "", "")
    delays = []
    for n in range(1, 8):
        sync.consecutive_failures = n
        delays.append(service.delay_after(failed))
    assert delays == [60, 120, 240, 480, 900, 900, 900]


async def test_unconfirmed_create_is_found_instead_of_duplicated(notion_world):
    w = notion_world
    camp = w.campaign()
    w.contact("Анна", PHONE_A, [camp])
    # Notion saves the page but answers 503 without saying so.
    w.mock.fail("POST", r"^/v1/pages$", 503, "service_unavailable", apply_first=True)
    first = await w.run()
    assert first.status == "partial"
    assert first.stats["creates_unconfirmed"] == 1
    second = await w.run()
    assert second.stats["jobs_adopted"] == 1
    assert len(w.pages("jobs")) == 1
    assert w.repo.list_jobs()[0]["notion_page_id"] == w.pages("jobs")[0]["id"]


async def test_unconfirmed_create_that_failed_is_repeated_only_after_a_grace_period(notion_world):
    w = notion_world
    camp = w.campaign()
    w.contact("Анна", PHONE_A, [camp])
    w.mock.fail("POST", r"^/v1/pages$", 503, "service_unavailable")  # nothing saved
    assert (await w.run()).status == "partial"
    waiting = await w.run()
    assert waiting.stats["jobs_waiting_for_confirmation"] == 1
    assert w.pages("jobs") == []
    later = w.new_sync(uncertain_grace=0)
    result = await w.run(later)
    assert result.stats["jobs_created"] == 1
    assert len(w.pages("jobs")) == 1


async def test_committed_resource_id_is_recorded_without_a_second_create(notion_world):
    w = notion_world
    camp = w.campaign()
    w.contact("Анна", PHONE_A, [camp])
    w.mock.fail(
        "POST", r"^/v1/pages$", 503, "service_unavailable", apply_first=True, committed_id=True
    )
    result = await w.run()
    assert result.status == "ok", result
    assert result.stats["jobs_created"] == 1
    assert len(w.pages("jobs")) == 1
    assert w.repo.list_jobs()[0]["notion_page_id"] == w.pages("jobs")[0]["id"]


async def test_broken_schema_stops_the_sync_until_it_is_repaired(notion_world):
    w = notion_world
    camp = w.campaign()
    w.contact("Анна", PHONE_A, [camp])
    async with w.client() as client:
        await client.update_data_source(w.ds["jobs"], {"properties": {"Ключ": None}})
    writes = w.writes()
    result = await w.run()
    assert (result.status, result.error_code) == ("failed", "notion_setup_error")
    assert "Ключ" in result.error_message
    assert w.writes() == writes

    async with w.client() as client:
        workspace = await resolve_workspace(client, w.settings.notion_databases, repair=True)
    assert workspace.problems() == []
    assert (await w.run()).status == "ok"


async def test_missing_select_option_is_added_by_writing_it(notion_world):
    w = notion_world
    async with w.client() as client:
        ds = await client.retrieve_data_source(w.ds["jobs"])
        options = ds["properties"]["Статус"]["select"]["options"]
        keep = [{"id": o["id"]} for o in options if o["name"] != "В очереди"]
        await client.update_data_source(
            w.ds["jobs"], {"properties": {"Статус": {"select": {"options": keep}}}}
        )
    camp = w.campaign()
    w.contact("Анна", PHONE_A, [camp])
    result = await w.run()
    assert result.status == "ok", result
    assert w.rows("jobs")[0]["Статус"] == "В очереди"


async def test_another_sync_holding_the_lock_is_respected(notion_world):
    w = notion_world
    assert w.repo.db.acquire_lock(LOCK_NAME, "another-process", 300)
    w.mock.requests.clear()
    result = await w.run()
    assert (result.status, result.error_code) == ("skipped", "sync_already_running")
    assert w.mock.requests == []
    w.repo.db.release_lock(LOCK_NAME, "another-process")
    assert (await w.run()).status == "ok"


async def test_contact_in_many_campaigns_is_read_completely(notion_world):
    w = notion_world
    w.mock.relation_limit = 2  # Notion shows 25 references per page object
    campaigns = [w.campaign(f"Кампания {i}") for i in range(3)]
    w.contact("Анна", PHONE_A, campaigns)
    result = await w.run()
    assert result.stats["relations_paginated"] == 1
    assert len(w.rows("jobs")) == 3


async def test_scans_beyond_the_query_result_cap(notion_world):
    w = notion_world
    w.mock._clock = stepping_clock(datetime(2026, 1, 1, tzinfo=UTC))
    w.mock.query_result_limit = 5
    camp = w.campaign()
    for i in range(12):
        w.contact(f"Контакт {i}", f"+99450123{4500 + i}", [camp])
    result = await w.run()
    assert result.status == "ok", result
    assert result.stats["contacts_seen"] == 12
    assert len(w.pages("jobs")) == 12
    assert "jobs_created" not in (await w.run()).stats


async def test_incomplete_scan_never_marks_contacts_as_deleted(notion_world):
    w = notion_world
    camp = w.campaign()
    for i in range(3):
        w.contact(f"Контакт {i}", f"+99450123{4500 + i}", [camp])
    await w.run()
    w.mock.query_result_limit = 2  # all pages share one created_time minute: cannot be split
    result = await w.run()
    assert result.status == "partial"
    assert any("incomplete" in p for p in result.problems)
    assert all(not c["missing_in_notion"] for c in w.repo.list_contacts())
    assert {row["Статус"] for row in w.rows("jobs")} == {"В очереди"}


async def test_sync_without_databases_or_token_fails_cleanly(make_settings, tmp_path):
    from wewln_phonecaller.notion.sync import NotionSync
    from wewln_phonecaller.storage import Database
    from wewln_phonecaller.storage.repository import Repository

    db = Database(tmp_path / "x.db")
    db.migrate()
    result = await NotionSync(make_settings(), Repository(db)).run_once()
    assert (result.status, result.error_code) == ("failed", "notion_token_missing")
    settings = make_settings(notion_token="ntn_" + "t" * 40)
    result = await NotionSync(settings, Repository(db)).run_once()
    assert (result.status, result.error_code) == ("failed", "notion_setup_error")
    assert "WEWLN_NOTION_CAMPAIGNS_DB" in result.error_message


async def test_logs_and_sync_records_contain_no_names_phones_or_tokens(notion_world, caplog):
    caplog.set_level(logging.DEBUG)
    w = notion_world
    camp = w.campaign()
    w.contact("Секретова Анна", PHONE_A, [camp])
    w.contact("Тайнов Борис", PHONE_B, [camp], dnc=True)
    await w.run()
    w.repo.simulate(limit=10)
    w.mock.fail("PATCH", r"^/v1/pages/", 400, "validation_error", times=3)
    results = [await w.run(), await w.run()]

    raw = "\n".join(
        json.dumps({k: str(v) for k, v in vars(r).items()}, ensure_ascii=False)
        for r in caplog.records
    )
    raw += json.dumps([r.as_dict() for r in results], ensure_ascii=False)
    raw += json.dumps(w.repo.recent_sync_runs(), ensure_ascii=False)
    for secret in ("Секретова", "Тайнов", PHONE_A, PHONE_A[1:], PHONE_B[4:], w.mock.token):
        assert secret not in raw
    assert "notion_sync_finished" in raw


def test_campaign_summary_text():
    assert campaign_summary({}, simulated=False) == "Заданий пока нет"
    text = campaign_summary({"queued": 2, "done": 1}, simulated=True)
    assert text == (
        "Заданий: 3 (В очереди: 2, Выполнено: 1). Режим симуляции: реальные звонки не выполняются"
    )
