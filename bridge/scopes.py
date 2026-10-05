"""Decision categories: the scope of a decision beyond the files it
touches. A billing policy question routed by file lands on whoever last
touched the metering code; routed by category it lands on whoever the
organization says decides billing. The categories are a small fixed
vocabulary an authority row can name (scope_kind 'category'); a question
is mapped to them by the words it uses, or by the category the agent
passed explicitly."""

from __future__ import annotations

import re

CATEGORIES: dict[str, str] = {
    "billing": r"\b(bill(?:ing|ed|s)?|invoic\w*|charg(?:e|es|ed|ing)|meter(?:ing|ed)?|usage[- ]based|overage|refund\w*|credit(?:s)?\b|prepaid)",
    "pricing": r"\b(pric(?:e|es|ed|ing)|discount\w*|tier(?:s|ed)?|plan(?:s)?\b|sku(?:s)?|rate card|minimums?|per[- ]seat)",
    "security": r"\b(securit\w*|vulnerab\w*|secret(?:s)?|token(?:s)?|credential\w*|encrypt\w*|cve[- ]?\d*|hardening)",
    "auth": r"\b(auth[nz]?|authentication|authoriz\w*|login|sso|oauth|permission(?:s)?|access control|rbac|roles?\b)",
    "privacy": r"\b(privac\w*|pii|gdpr|ccpa|personal data|consent|retention|data deletion|anonymi[sz]\w*)",
    "compat": r"\b(backwards?[- ]compatib\w*|compatibility|breaking change|breaks?\b|deprecat\w*|public api|migrat\w*|semver|preserv\w* (?:existing|current)|existing behaviou?r)",
    "rollout": r"\b(roll ?out|release(?:s|d)?\b|deploy\w*|launch\w*|feature flag|canary|stag(?:e|ed|ing)|cutover|ga\b)",
    "data": r"\b(schema|migration|database|table(?:s)?\b|column(?:s)?\b|index(?:es)?\b|backup(?:s)?|restore|retention)",
    "infra": r"\b(infra\w*|cluster|kubernetes|k8s|terraform|scheduler|cron|capacity|scaling|region(?:s)?|cost(?:s)?\b)",
    "legal": r"\b(licen[cs]\w*|contract(?:s|ual)?|terms of service|tos\b|legal|compliance|audit\w*)",
    "customer": r"\b(customer(?:s)?|account(?:s)?|enterprise|tenant(?:s)?|cohort|partner(?:s)?|sla\b)",
    "ux": r"\b(ux|ui\b|user[- ]facing|screen(?:s)?|button(?:s)?|flow(?:s)?|copy\b|wording|onboarding)",
    "ops": r"\b(incident(?:s)?|on[- ]call|pager|alert(?:s|ing)?|runbook|sla\b|outage|postmortem)",
    # What users are told. Measured live: "not me, the Release Manager owns
    # what we tell users" on a docs question named no category, and Raven
    # learned the file's whole directory instead.
    "docs": r"\b(docs?\b|documentation|docstring\w*|changelog\w*|release notes?|readme|what (?:we|to) tell users)",
}
_COMPILED = {name: re.compile(pattern, re.IGNORECASE) for name, pattern in CATEGORIES.items()}
_LADDER_CATEGORIES = {"definition", "policy", "data-source", "rollout", "compat", "ux", "ops"}


def decision_scopes(question: str, context: str = "", category: str = "", include_context: bool = True) -> list[str]:
    """The categories a decision falls under, most specific first: the
    category the agent named when it is one of the vocabulary, then
    every category whose words the question uses, then those the
    context uses (matching is broad; learning from an answer or a
    referral passes include_context=False, since a context that
    mentions a rollout does not make the decision a rollout decision).
    Ladder categories that are not decision scopes (policy, definition,
    data-source) name none."""
    out: list[str] = []
    cat = (category or "").strip().lower()
    if cat and cat in CATEGORIES:
        out.append(cat)
    for text in (question or "", context or "" if include_context else ""):
        if not text:
            continue
        for name, rx in _COMPILED.items():
            if name not in out and rx.search(text):
                out.append(name)
    return out


def primary_scopes(question: str, category: str = "") -> list[str]:
    """What a decision is about, for the two things a topic must never be
    guessed into: standing to approve it, and a route learned from a
    hand-on. The category the agent named, when it is one of the
    vocabulary, is the whole answer; otherwise the categories the
    question's own words name. The context is not read. Measured live: a
    docs question asking whether the release notes should call a change
    "a security fix" gave the person it was handed to standing on every
    security decision in the repository."""
    cat = (category or "").strip().lower()
    if cat in CATEGORIES:
        return [cat]
    return decision_scopes(question, "", "", include_context=False)


def known_category(name: str) -> bool:
    return (name or "").strip().lower() in CATEGORIES
