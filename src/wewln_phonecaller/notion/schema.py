"""The four Notion databases used by wewln: names, properties, types and select options.

Property names are part of the contract: the sync finds properties by name, so renaming one in
Notion breaks the sync until it is renamed back (`wewln notion check` shows what is wrong).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import SUPPORTED_LANGUAGES
from ..domain import CAMPAIGN_STATUS_LABELS, JOB_STATUS_LABELS, OUTCOME_LABELS

# Property names (Russian, as shown in Notion).
TITLE = "title"
CAMPAIGN_NAME = "Название"
CAMPAIGN_STATUS = "Статус"
CAMPAIGN_SCRIPT = "Сценарий"
CAMPAIGN_LANGUAGE = "Язык"
CAMPAIGN_MAX_ATTEMPTS = "Макс. попыток"
CAMPAIGN_SUMMARY = "Сводка"
CAMPAIGN_ERROR = "Ошибка"

CONTACT_NAME = "Имя"
CONTACT_PHONE = "Телефон"
CONTACT_DNC = "Не звонить"
CONTACT_DNC_REASON = "Причина запрета"
CONTACT_CAMPAIGNS = "Кампании"
CONTACT_ERROR = "Ошибка"

JOB_TITLE = "Задание"
JOB_CAMPAIGN = "Кампания"
JOB_CONTACT = "Контакт"
JOB_STATUS = "Статус"
JOB_REASON = "Причина"
JOB_ATTEMPTS = "Попытки"
JOB_KEY = "Ключ"
JOB_UPDATED = "Обновлено"
JOB_SIMULATED = "Симуляция"

RESULT_TITLE = "Результат"
RESULT_JOB = "Задание"
RESULT_CAMPAIGN = "Кампания"
RESULT_CONTACT = "Контакт"
RESULT_OUTCOME = "Исход"
RESULT_STARTED = "Начало"
RESULT_DURATION = "Длительность, с"
RESULT_SUMMARY = "Итог"
RESULT_KEY = "Ключ"
RESULT_SIMULATED = "Симуляция"

_COLORS = ("gray", "blue", "green", "yellow", "red", "purple", "orange", "brown", "pink")


@dataclass(frozen=True)
class PropertySpec:
    name: str
    type: str
    options: tuple[str, ...] = ()
    # For relations: key of the target database (see DATABASES).
    target: str | None = None

    def config(self, data_source_ids: dict[str, str]) -> dict[str, Any]:
        """Property configuration for create/update requests."""
        if self.type == "select":
            options = [
                {"name": name, "color": _COLORS[i % len(_COLORS)]}
                for i, name in enumerate(self.options)
            ]
            return {"select": {"options": options}}
        if self.type == "number":
            return {"number": {"format": "number"}}
        if self.type == "relation":
            if self.target is None:
                raise ValueError(f"relation {self.name} has no target")
            return {
                "relation": {
                    "data_source_id": data_source_ids[self.target],
                    "type": "single_property",
                    "single_property": {},
                }
            }
        return {self.type: {}}


@dataclass(frozen=True)
class DatabaseSpec:
    key: str
    title: str
    setting: str
    env_var: str
    properties: tuple[PropertySpec, ...]
    description: str = ""
    dependencies: tuple[str, ...] = field(default=())

    @property
    def title_property(self) -> str:
        return next(p.name for p in self.properties if p.type == "title")

    def initial_properties(self, data_source_ids: dict[str, str]) -> dict[str, Any]:
        return {p.name: p.config(data_source_ids) for p in self.properties}


CAMPAIGNS = DatabaseSpec(
    key="campaigns",
    title="Кампании (wewln)",
    setting="notion_campaigns_db",
    env_var="WEWLN_NOTION_CAMPAIGNS_DB",
    description="Кампании обзвона. «Статус» управляет кампанией; «Сводку» и «Ошибку» пишет wewln.",
    properties=(
        PropertySpec(CAMPAIGN_NAME, "title"),
        PropertySpec(CAMPAIGN_STATUS, "select", tuple(CAMPAIGN_STATUS_LABELS.values())),
        PropertySpec(CAMPAIGN_SCRIPT, "rich_text"),
        PropertySpec(CAMPAIGN_LANGUAGE, "select", tuple(SUPPORTED_LANGUAGES)),
        PropertySpec(CAMPAIGN_MAX_ATTEMPTS, "number"),
        PropertySpec(CAMPAIGN_SUMMARY, "rich_text"),
        PropertySpec(CAMPAIGN_ERROR, "rich_text"),
    ),
)
CONTACTS = DatabaseSpec(
    key="contacts",
    title="Контакты (wewln)",
    setting="notion_contacts_db",
    env_var="WEWLN_NOTION_CONTACTS_DB",
    description="Контакты. Отметка «Не звонить» блокирует любые звонки на этот номер.",
    properties=(
        PropertySpec(CONTACT_NAME, "title"),
        PropertySpec(CONTACT_PHONE, "phone_number"),
        PropertySpec(CONTACT_DNC, "checkbox"),
        PropertySpec(CONTACT_DNC_REASON, "rich_text"),
        PropertySpec(CONTACT_CAMPAIGNS, "relation", target="campaigns"),
        PropertySpec(CONTACT_ERROR, "rich_text"),
    ),
    dependencies=("campaigns",),
)
JOBS = DatabaseSpec(
    key="jobs",
    title="Задания на звонки (wewln)",
    setting="notion_jobs_db",
    env_var="WEWLN_NOTION_JOBS_DB",
    description="Задания создаёт и обновляет wewln. Статус «Отменено» можно поставить вручную.",
    properties=(
        PropertySpec(JOB_TITLE, "title"),
        PropertySpec(JOB_CAMPAIGN, "relation", target="campaigns"),
        PropertySpec(JOB_CONTACT, "relation", target="contacts"),
        PropertySpec(JOB_STATUS, "select", tuple(JOB_STATUS_LABELS.values())),
        PropertySpec(JOB_REASON, "rich_text"),
        PropertySpec(JOB_ATTEMPTS, "number"),
        PropertySpec(JOB_KEY, "rich_text"),
        PropertySpec(JOB_UPDATED, "date"),
        PropertySpec(JOB_SIMULATED, "checkbox"),
    ),
    dependencies=("campaigns", "contacts"),
)
RESULTS = DatabaseSpec(
    key="results",
    title="Результаты звонков (wewln)",
    setting="notion_results_db",
    env_var="WEWLN_NOTION_RESULTS_DB",
    description="Результаты звонков (в этой версии только симуляция). Записи создаёт wewln.",
    properties=(
        PropertySpec(RESULT_TITLE, "title"),
        PropertySpec(RESULT_JOB, "relation", target="jobs"),
        PropertySpec(RESULT_CAMPAIGN, "relation", target="campaigns"),
        PropertySpec(RESULT_CONTACT, "relation", target="contacts"),
        PropertySpec(RESULT_OUTCOME, "select", tuple(OUTCOME_LABELS.values())),
        PropertySpec(RESULT_STARTED, "date"),
        PropertySpec(RESULT_DURATION, "number"),
        PropertySpec(RESULT_SUMMARY, "rich_text"),
        PropertySpec(RESULT_KEY, "rich_text"),
        PropertySpec(RESULT_SIMULATED, "checkbox"),
    ),
    dependencies=("jobs", "campaigns", "contacts"),
)

# Creation order matters: relations need their target data source first.
DATABASES: tuple[DatabaseSpec, ...] = (CAMPAIGNS, CONTACTS, JOBS, RESULTS)
DATABASES_BY_KEY = {spec.key: spec for spec in DATABASES}


@dataclass
class SchemaReport:
    """Differences between a data source and its spec."""

    key: str
    missing: list[PropertySpec] = field(default_factory=list)
    wrong_type: list[tuple[str, str, str]] = field(default_factory=list)  # name, expected, actual
    missing_options: dict[str, list[str]] = field(default_factory=dict)
    wrong_target: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.wrong_type or self.missing_options or self.wrong_target)

    @property
    def fixable(self) -> bool:
        """Missing properties and options can be added; wrong types are never changed."""
        return not (self.wrong_type or self.wrong_target)

    def problems(self) -> list[str]:
        spec = DATABASES_BY_KEY[self.key]
        lines = [f"{spec.title}: missing property «{p.name}» ({p.type})" for p in self.missing]
        lines += [
            f"{spec.title}: property «{name}» must be {expected}, found {actual}"
            for name, expected, actual in self.wrong_type
        ]
        lines += [
            f"{spec.title}: property «{name}» lacks options: " + ", ".join(options)
            for name, options in self.missing_options.items()
        ]
        lines += [
            f"{spec.title}: relation «{name}» points to another database"
            for name in self.wrong_target
        ]
        return lines


def compare_schema(
    spec: DatabaseSpec, data_source: dict[str, Any], data_source_ids: dict[str, str]
) -> SchemaReport:
    report = SchemaReport(spec.key)
    properties: dict[str, Any] = data_source.get("properties") or {}
    for prop in spec.properties:
        actual = properties.get(prop.name)
        if prop.type == "title":
            # Every data source has exactly one title property; its name may differ.
            actual = actual or next(
                (v for v in properties.values() if v.get("type") == "title"), None
            )
        if actual is None:
            report.missing.append(prop)
            continue
        if actual.get("type") != prop.type:
            report.wrong_type.append((prop.name, prop.type, str(actual.get("type"))))
            continue
        if prop.type == "select":
            present = {o.get("name") for o in (actual.get("select") or {}).get("options") or []}
            lacking = [o for o in prop.options if o not in present]
            if lacking:
                report.missing_options[prop.name] = lacking
        if prop.type == "relation" and prop.target in data_source_ids:
            target = (actual.get("relation") or {}).get("data_source_id")
            if target and not _same_id(target, data_source_ids[prop.target]):
                report.wrong_target.append(prop.name)
    return report


def repair_body(
    report: SchemaReport, data_source: dict[str, Any], data_source_ids: dict[str, str]
) -> dict[str, Any] | None:
    """PATCH body that adds missing properties and select options (existing ones are kept)."""
    if report.ok or not report.fixable:
        return None
    spec = DATABASES_BY_KEY[report.key]
    body: dict[str, Any] = {}
    for prop in report.missing:
        if prop.type == "title":
            continue
        body[prop.name] = prop.config(data_source_ids)
    existing = data_source.get("properties") or {}
    for name, lacking in report.missing_options.items():
        options = [{"id": o["id"]} for o in existing[name]["select"].get("options") or []]
        spec_prop = next(p for p in spec.properties if p.name == name)
        offset = len(options)
        options += [
            {"name": o, "color": _COLORS[(offset + i) % len(_COLORS)]}
            for i, o in enumerate(lacking)
            if o in spec_prop.options
        ]
        body[name] = {"select": {"options": options}}
    return {"properties": body} if body else None


def title_property_name(data_source: dict[str, Any], spec: DatabaseSpec) -> str:
    for name, value in (data_source.get("properties") or {}).items():
        if value.get("type") == "title":
            return name
    return spec.title_property


def _same_id(a: str, b: str) -> bool:
    return a.replace("-", "").lower() == b.replace("-", "").lower()
