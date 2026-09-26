"""Plain-English labels for values the UI shows a human.

Every string here exists because an internal identifier was leaking onto the
screen. Enum values like "pattern_confirmed", collector names like "hn_hiring",
and signal kinds like "integration_detected" are fine in the database and in
logs — they are precise and greppable. They are not fine in a table cell that a
non-engineer reads every morning to decide who to email.

One module rather than inline Jinja conditionals because the same status was
already rendering three different ways across board.html and detail.html.
"""
from __future__ import annotations

# --- email trust -----------------------------------------------------------
# The only thing the user actually needs from a contact's status: can I send to
# this right now, or should I check it first?
EMAIL_STATUS = {
    "valid":             ("Verified",   "ok",    "Confirmed deliverable. Safe to send."),
    "pattern_confirmed": ("Confirmed",  "ok",    "They published this address themselves. Safe to send."),
    "guessed":           ("Best guess", "warm",  "Built from this company's email pattern. Verify before sending."),
    "guessed_no_pattern": ("Unverified", "warm", "We had no known address to learn their format from. Verify first."),
    "unverified":        ("Unverified", "muted", "Not checked yet."),
    "unknown":           ("Unclear",    "muted", "Their mail server wouldn't confirm either way."),
    "catch_all":         ("Risky",      "warm",  "This domain accepts any address, so delivery isn't guaranteed."),
    "invalid":           ("Bad",        "bad",   "This address doesn't exist. Don't send."),
}


def email_status(status: str | None) -> tuple[str, str, str]:
    """(label, css_class, explanation) for a contact's email status."""
    return EMAIL_STATUS.get(status or "", (status or "Unknown", "muted", ""))


# --- what the post looked like ---------------------------------------------
POST_LABEL = {
    "stated_intent":    ("Ready to buy",  "intent"),
    "competitor_gripe": ("Unhappy with a rival", "intent"),
    "active_research":  ("Looking around", ""),
    "none":             ("Not relevant",  ""),
}


def post_label(label: str | None) -> tuple[str, str]:
    return POST_LABEL.get(label or "", ("Not sorted yet", ""))


# --- why an account is on the board ----------------------------------------
LEAD_TYPE = {
    "company": ("Something happened", "company",
                "A recent event worth mentioning — a hire, an outage, funding."),
    "intent":  ("Actively looking",   "intent",
                "They've said something in public that sounds like buying interest."),
    "cold":    ("Good fit, quiet",    "cold",
                "Matches your ideal customer, but nothing is happening right now."),
}


def lead_type(t: str) -> tuple[str, str, str]:
    return LEAD_TYPE.get(t, (t, "", ""))


# --- where a signal came from ----------------------------------------------
# Collector codenames are how the scheduler and logs refer to these. The user
# only needs to know which real-world place the information came from.
SOURCE = {
    "ats":        "Their job board",
    "hn_hiring":  "Hacker News hiring thread",
    "yc":         "Y Combinator directory",
    "techstack":  "Their website",
    "statuspage": "Their status page",
    "vendors":    "A vendor's customer list",
    "jobfeeds":   "Remote job boards",
    "edgar":      "SEC filings",
    "hn":         "Hacker News",
    "mastodon":   "Mastodon",
    "devto":      "Dev.to",
    "lobsters":   "Lobsters",
    "github":     "GitHub",
    "stargazers": "GitHub stars",
    "reddit":     "Reddit",
    "show_hn":    "Hacker News launch posts",
    "wwr":        "We Work Remotely",
    "producthunt": "Product Hunt",
    "contacts":   "GitHub commit history",
    "tracking":   "Your website",
    "manual":     "You added this",
}


def source(name: str | None) -> str:
    n = (name or "").split(":")[0]          # "llm:hn" -> "hn"
    return SOURCE.get(n, n or "Unknown")


# --- what kind of signal it is ---------------------------------------------
SIGNAL_KIND = {
    "major_incident":           "Major outage",
    "recent_incident":          "Recent outage",
    "incident_streak":          "Repeated outages",
    "new_status_page":          "New status page",
    "status_page":              "Runs a status page",
    "first_sre_hire":           "Hiring their first ops engineer",
    "first_em_hire":            "Hiring their first eng manager",
    "first_release_hire":       "Hiring their first release/QA engineer",
    "first_security_hire":      "Hiring their first security engineer",
    "eng_req_surge":            "Hiring engineers fast",
    "hiring_publicly":          "Hiring publicly",
    "funding_seed_a":           "Raised funding",
    "stated_intent":            "Said they're looking",
    "competitor_gripe":         "Complained about a rival",
    "persona_pain_post":        "Described this exact problem",
    "persona_pain_match":       "Job ad describes this problem",
    "competitor_evaluation":    "Checking out a rival",
    "active_research":          "Reading up on the topic",
    "site_visit_pricing":       "Visited your pricing page",
    "site_visit":               "Visited your site",
    "integration_detected":     "Uses a tool you connect to",
    "owns_production_language": "Runs their own production",
    "competitor_complementary": "Already pays for similar tools",
    "competitor_displacement":  "Uses a direct rival",
    "multi_line":               "Tools spread across categories",
    "headcount_band":           "Right company size",
    "contacts_found":           "Found work emails",
    "discovered":               "Added to your list",
    "stack_evidence":           "Other tools they use",
}


def signal_kind(kind: str | None) -> str:
    k = kind or ""
    return SIGNAL_KIND.get(k, k.replace("_", " ").capitalize())


# --- the two halves of a score ---------------------------------------------
# --- the manual "check now" buttons ----------------------------------------
# Ordered by how much each one actually contributes, not alphabetically or by
# internal name. Reddit is omitted: it's disabled by default in malveon.yaml.
SOURCE_BUTTONS = [
    ("ats",        "Job boards",        "Reads companies' job pages for hiring signals and the tools they mention"),
    ("statuspage", "Status pages",      "Checks for recent outages — a strong reason to reach out now"),
    ("techstack",  "Company websites",  "Looks at each website to see which tools they run"),
    ("hn_hiring",  "Hiring threads",    "Finds companies posting in Hacker News hiring threads"),
    ("contacts",   "Work emails",       "Looks for publicly published work email addresses"),
    ("hn",         "Hacker News",       "Finds people discussing the problem Malveon solves"),
    ("mastodon",   "Mastodon",          "Same, on Mastodon"),
    ("devto",      "Dev.to",            "Same, on Dev.to articles"),
    ("github",     "GitHub",            "Finds people raising issues on competing tools"),
    ("yc",         "Y Combinator",      "Adds companies from the YC directory"),
    ("vendors",    "Vendor customers",  "Finds companies listed on your integration partners' sites"),
    ("edgar",      "Funding filings",   "Checks SEC filings for recently funded companies"),
    ("lobsters",   "Lobsters",          "Finds relevant discussion on Lobsters"),
    ("stargazers", "GitHub stars",      "Finds people starring competing tools"),
    ("jobfeeds",   "Remote job boards", "Reads remote job listings for extra hiring signals"),
    ("show_hn",    "HN launches",       "Adds companies showing off a new product on Hacker News"),
    ("wwr",        "We Work Remotely",  "Adds companies posting jobs on We Work Remotely"),
    ("producthunt", "Product Hunt",     "Adds companies launching on Product Hunt (needs a key — see Technical details)"),
]


def source_buttons() -> list[dict]:
    return [{"key": k, "label": l, "help": h} for k, l, h in SOURCE_BUTTONS]


FIT_HELP = "How closely they match the kind of company that buys Malveon."
INTENT_HELP = "How much is happening right now that gives you a reason to reach out."
SCORE_HELP = "Higher means contact them sooner. Combines how well they fit with what's happening now."
