"""Domain vocabulary shared by storage, the planner and Notion: statuses, reasons, Russian labels.

Codes (English) are stored in SQLite and never change; labels (Russian) are what Notion shows.
"""

from __future__ import annotations

from enum import StrEnum


class CampaignStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"


# Stopped and completed are final: a campaign never runs again (create a new one instead).
FINAL_CAMPAIGN_STATUSES = frozenset({CampaignStatus.STOPPED, CampaignStatus.COMPLETED})

CAMPAIGN_STATUS_LABELS: dict[CampaignStatus, str] = {
    CampaignStatus.DRAFT: "Черновик",
    CampaignStatus.RUNNING: "Запущена",
    CampaignStatus.PAUSED: "Пауза",
    CampaignStatus.STOPPED: "Остановлена",
    CampaignStatus.COMPLETED: "Завершена",
}
CAMPAIGN_STATUS_BY_LABEL = {label: status for status, label in CAMPAIGN_STATUS_LABELS.items()}


class JobStatus(StrEnum):
    QUEUED = "queued"
    BLOCKED = "blocked"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


OPEN_JOB_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.BLOCKED})
FINAL_JOB_STATUSES = frozenset({JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED})

JOB_STATUS_LABELS: dict[JobStatus, str] = {
    JobStatus.QUEUED: "В очереди",
    JobStatus.BLOCKED: "Заблокировано",
    JobStatus.IN_PROGRESS: "Выполняется",
    JobStatus.DONE: "Выполнено",
    JobStatus.FAILED: "Ошибка",
    JobStatus.CANCELLED: "Отменено",
}
JOB_STATUS_BY_LABEL = {label: status for status, label in JOB_STATUS_LABELS.items()}


class JobReason(StrEnum):
    DNC = "dnc"
    PHONE_MISSING = "phone_missing"
    PHONE_INVALID = "phone_invalid"
    PHONE_NEEDS_COUNTRY_CODE = "phone_needs_country_code"
    CAMPAIGN_STOPPED = "campaign_stopped"
    CAMPAIGN_COMPLETED = "campaign_completed"
    CONTACT_REMOVED = "contact_removed"
    CONTACT_DELETED = "contact_deleted"
    CANCELLED_IN_NOTION = "cancelled_in_notion"
    PAGE_DELETED_IN_NOTION = "page_deleted_in_notion"
    STATE_LOST = "state_lost"
    SIMULATED = "simulated"
    REGION_NOT_ALLOWED = "region_not_allowed"
    NUMBER_TYPE_NOT_ALLOWED = "number_type_not_allowed"
    RETRY_WAIT = "retry_wait"
    EMERGENCY_STOP = "emergency_stop"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    CALL_REJECTED = "call_rejected"
    NUMBER_UNREACHABLE = "number_unreachable"
    PROVIDER_ERROR = "provider_error"
    CANCELLED_LOCALLY = "cancelled_locally"


# A job cancelled for these reasons comes back when the contact returns to the campaign.
REACTIVATABLE_REASONS = frozenset({JobReason.CONTACT_REMOVED, JobReason.CONTACT_DELETED})

JOB_REASON_LABELS: dict[JobReason, str] = {
    JobReason.DNC: "Номер в списке «Не звонить»",
    JobReason.PHONE_MISSING: "Не указан номер телефона",
    JobReason.PHONE_INVALID: "Номер телефона не распознан",
    JobReason.PHONE_NEEDS_COUNTRY_CODE: "Номер без кода страны",
    JobReason.CAMPAIGN_STOPPED: "Кампания остановлена",
    JobReason.CAMPAIGN_COMPLETED: "Кампания завершена",
    JobReason.CONTACT_REMOVED: "Контакт убран из кампании",
    JobReason.CONTACT_DELETED: "Контакт удалён или недоступен",
    JobReason.CANCELLED_IN_NOTION: "Отменено вручную в Notion",
    JobReason.PAGE_DELETED_IN_NOTION: "Страница задания удалена в Notion",
    JobReason.STATE_LOST: "Состояние неизвестно после восстановления базы: проверьте вручную",
    JobReason.SIMULATED: "Симуляция: реальный звонок не выполнялся",
    JobReason.REGION_NOT_ALLOWED: "Страна номера не разрешена (WEWLN_CALL_REGIONS)",
    JobReason.NUMBER_TYPE_NOT_ALLOWED: "Платный или служебный номер: такие номера не набираются",
    JobReason.RETRY_WAIT: "Ждёт повторного звонка",
    JobReason.EMERGENCY_STOP: "Аварийная остановка: звонок прерван, задание ждёт возобновления",
    JobReason.ATTEMPTS_EXHAUSTED: "Не дозвонились: попытки исчерпаны (исход — в «Результатах»)",
    JobReason.CALL_REJECTED: "Абонент отклонил звонок",
    JobReason.NUMBER_UNREACHABLE: "Номер не существует или недоступен",
    JobReason.PROVIDER_ERROR: "Ошибка телефонии: проверьте настройки провайдера",
    JobReason.CANCELLED_LOCALLY: "Звонок отменён вручную в wewln",
}

# A queued job with one of these reasons is still simply queued: the note explains the wait.
QUEUED_NOTE_REASONS = frozenset(
    {JobReason.RETRY_WAIT, JobReason.EMERGENCY_STOP, JobReason.PROVIDER_ERROR}
)

JOB_REASON_BY_LABEL = {label: reason for reason, label in JOB_REASON_LABELS.items()}

PHONE_ERROR_REASONS = {
    "missing": JobReason.PHONE_MISSING,
    "invalid": JobReason.PHONE_INVALID,
    "needs_country_code": JobReason.PHONE_NEEDS_COUNTRY_CODE,
    "region_not_allowed": JobReason.REGION_NOT_ALLOWED,
    "number_type_not_allowed": JobReason.NUMBER_TYPE_NOT_ALLOWED,
}


class Outcome(StrEnum):
    SIMULATED = "simulated"
    ANSWERED = "answered"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    OPT_OUT = "opt_out"
    REJECTED = "rejected"
    UNREACHABLE = "unreachable"
    CANCELLED = "cancelled"


# New labels reach existing Notion databases by themselves: Notion adds a select option when a
# page is written with it (`wewln notion check --repair` adds them in advance).
OUTCOME_LABELS: dict[Outcome, str] = {
    Outcome.SIMULATED: "Симуляция",
    Outcome.ANSWERED: "Ответ",
    Outcome.NO_ANSWER: "Нет ответа",
    Outcome.BUSY: "Занято",
    Outcome.FAILED: "Ошибка",
    Outcome.OPT_OUT: "Отказ",
    Outcome.REJECTED: "Отклонён",
    Outcome.UNREACHABLE: "Номер недоступен",
    Outcome.CANCELLED: "Отменён",
}


class SuppressionSource(StrEnum):
    MANUAL = "manual"
    NOTION = "notion"
    OPT_OUT = "opt_out"


def reason_label(code: str | None) -> str:
    if not code:
        return ""
    try:
        return JOB_REASON_LABELS[JobReason(code)]
    except ValueError:
        return code
