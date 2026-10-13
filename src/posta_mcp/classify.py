"""Sender categories for the Boğaziçi summaries: list, standard, student, faculty, external.

Rules come from the preset (``Config.classification`` and ``Config.domains``), so the user can
override any of them in ``config.toml``.  Precedence, first match wins:

1. ``list``     - a ``List-Id`` header, or the sender / a recipient is a list address or in a
                  list domain.  A faculty member writing *through* a list is ``list``.
2. ``standard`` - automated or institutional-service sender: the exact address is listed
                  (``bilgiislem@...``), or the local part is an automated name (``noreply``,
                  ``mailer-daemon``) at an institution or student domain.
3. ``student``  - sender domain (or a subdomain of it) is a student domain.
4. ``faculty``  - sender domain (or a subdomain, e.g. ``pt.``/``alum.``) is an institution
                  domain and none of the above applied: "likely a professor".
5. ``external`` - anything else.

Everything is case-insensitive.  ``Classification.reason`` names the rule that matched and never
quotes message content, so it is safe to show next to untrusted data.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from email.utils import getaddresses
from typing import Any, Literal

from posta_mcp.models import MessageSummary

Category = Literal["list", "standard", "student", "faculty", "external"]
CATEGORIES: tuple[Category, ...] = ("list", "standard", "student", "faculty", "external")

_SEPARATORS = re.compile(r"[-_.]")


def _lower_all(values: Iterable[str] | None) -> frozenset[str]:
    return frozenset(v.strip().lower().lstrip("@") for v in (values or ()) if v and v.strip())


def _squash(local: str) -> str:
    """``no-reply`` / ``no_reply`` / ``noreply`` compare equal."""
    return _SEPARATORS.sub("", local.lower())


@dataclass(frozen=True)
class Rules:
    """Lower-cased classification rules."""

    list_addresses: frozenset[str] = frozenset()
    list_domains: frozenset[str] = frozenset()
    standard_addresses: frozenset[str] = frozenset()
    standard_local_parts: frozenset[str] = frozenset()  # separator-insensitive
    student_domains: frozenset[str] = frozenset()
    institution_domains: frozenset[str] = frozenset()

    @classmethod
    def build(
        cls,
        *,
        list_addresses: Iterable[str] | None = None,
        list_domains: Iterable[str] | None = None,
        standard_addresses: Iterable[str] | None = None,
        standard_local_parts: Iterable[str] | None = None,
        student_domains: Iterable[str] | None = None,
        institution_domains: Iterable[str] | None = None,
    ) -> Rules:
        return cls(
            _lower_all(list_addresses),
            _lower_all(list_domains),
            _lower_all(standard_addresses),
            frozenset(_squash(p) for p in _lower_all(standard_local_parts)),
            _lower_all(student_domains),
            _lower_all(institution_domains),
        )

    @classmethod
    def from_config(cls, config: Any) -> Rules:
        """Read the rules from a ``Config``.  Missing sections or fields just mean no rules."""
        cls_ = getattr(config, "classification", None)
        dom = getattr(config, "domains", None)
        return cls.build(
            list_addresses=getattr(cls_, "list_addresses", None),
            list_domains=getattr(cls_, "list_domains", None),
            standard_addresses=getattr(cls_, "standard_addresses", None),
            standard_local_parts=getattr(cls_, "standard_local_parts", None),
            student_domains=getattr(dom, "student", None),
            institution_domains=getattr(dom, "institution", None),
        )


@dataclass(frozen=True)
class Classification:
    category: Category
    reason: str

    def __str__(self) -> str:
        return self.category


def _split(addr: str) -> tuple[str, str]:
    local, _, domain = addr.strip().strip("<>").lower().rpartition("@")
    return local, domain


def in_domains(domain: str, domains: frozenset[str]) -> bool:
    """``domain`` equals one of ``domains`` or is a subdomain of one."""
    domain = domain.rstrip(".")
    return any(domain == d or domain.endswith("." + d) for d in domains)


def _recipient_addresses(summary: MessageSummary) -> list[str]:
    return [a.lower() for _n, a in getaddresses([*summary.to, *summary.cc]) if a]


def classify_with_reason(msg: MessageSummary, rules: Rules) -> Classification:
    """Classify a ``MessageSummary`` (or ``ParsedMessage``) and say why."""
    sender = (msg.from_addr or "").strip().lower()
    local, domain = _split(sender)

    if msg.list_id and msg.list_id.strip():
        return Classification("list", "has a List-Id header")
    if sender in rules.list_addresses or (domain and in_domains(domain, rules.list_domains)):
        return Classification("list", "sender is a mailing-list address")
    for rcpt in _recipient_addresses(msg):
        r_domain = rcpt.rpartition("@")[2]
        if rcpt in rules.list_addresses or in_domains(r_domain, rules.list_domains):
            return Classification("list", "addressed to a mailing-list address")

    if sender in rules.standard_addresses:
        return Classification("standard", "sender is a listed standard address")
    is_student = bool(domain) and in_domains(domain, rules.student_domains)
    is_staff = bool(domain) and in_domains(domain, rules.institution_domains)
    # Automated-sender names only count inside the institution: noreply@some-shop.example is
    # not "school mail" (it must not be treated as institutional by the personalised summary).
    if (
        local
        and (is_student or is_staff)
        and _squash(local.partition("+")[0]) in rules.standard_local_parts
    ):
        return Classification("standard", "sender local part is an automated-sender name")

    if is_student:
        return Classification("student", "sender is in a student domain")
    if is_staff:
        return Classification("faculty", "sender is in an institution domain (likely staff)")
    return Classification("external", "sender is outside the institution")


def classify(msg: MessageSummary, rules: Rules) -> Category:
    return classify_with_reason(msg, rules).category
