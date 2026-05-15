"""Base contract for site-specific crawlers.

A crawler is responsible for:
  1. Returning the source/category metadata it represents.
  2. Yielding announcement dicts ready to be passed into `db.upsert_announcement`.
  3. For each announcement, optionally yielding attachment dicts.

Crawlers don't talk to the DB directly — `run_crawler.py` orchestrates that so
the upsert/transaction behavior stays consistent across sources.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class CategorySpec:
    code: str
    name: str
    list_url: str | None = None


@dataclass
class SourceSpec:
    code: str
    name: str
    base_url: str
    categories: list[CategorySpec] = field(default_factory=list)


@dataclass
class AnnouncementRecord:
    """Normalized announcement payload (공고/공모) that a crawler emits."""
    category_code: str
    external_id: str
    title: str
    detail_url: str
    external_biz_id: str | None = None
    status: str | None = None
    d_day: str | None = None
    reg_date: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    department: str | None = None
    contact: str | None = None
    content_html: str | None = None
    content_text: str | None = None
    raw_meta: dict | None = None
    attachments: list[dict] = field(default_factory=list)
    # When this announcement belongs to a 보조사업 master record
    business_external_id: str | None = None
    business_bsns_year: str | None = None


@dataclass
class AssistanceBusinessRecord:
    """Normalized 보조사업 (assistance business) master record."""
    category_code: str
    external_id: str
    name: str
    bsns_year: str | None = None
    parent_name: str | None = None
    purpose: str | None = None
    overview: str | None = None
    legal_basis: str | None = None
    support_target: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    department: str | None = None
    sub_dept: str | None = None
    charger_name: str | None = None
    charger_tel: str | None = None
    charger_email: str | None = None
    sub_count: int | None = None
    classification: str | None = None
    business_type: str | None = None
    delivery_type: str | None = None
    settlement_type: str | None = None
    portal_url: str | None = None
    detail_url: str | None = None
    raw_meta: dict | None = None
    attributes: list[dict] = field(default_factory=list)
    # 0..N announcements (공모) that hang off this business
    announcements: list[AnnouncementRecord] = field(default_factory=list)


class BaseCrawler:
    source: SourceSpec

    def crawl(self):
        """Yield AnnouncementRecord and/or AssistanceBusinessRecord instances."""
        raise NotImplementedError
