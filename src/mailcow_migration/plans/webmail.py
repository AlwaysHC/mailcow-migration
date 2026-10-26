import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from .assessment import check
from .domain import active
from .forwarding import mailbox_filter_plans, sieve_string
from .native_settings import mailbox_sieve, native_mailbox_settings, utc_date, vacation_days, vacation_timestamp, valid_vacation_days
from .sieve import SieveMappingError
from .sieve_evidence import SieveEvidence, sieve_inventory_blockers
from ..core.errors import MigrationError
from ..core.storage import json_fingerprint
from ..core.timezones import timezone_parts
from ..sources import SourceKind


def sieve_body(value: str) -> str:
    if not isinstance(value, str) or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        raise MigrationError("Unsupported Sieve body text")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return "text:\n" + re.sub(r"^\.", "..", normalized, flags=re.M) + "\n.\n"


def vacation_prefilter(row: Mapping[str, Any], start: int, end: int, days: int) -> Mapping[str, Any]:
    values = {key: int(value) if isinstance(value, float) and value.is_integer() else value for key, value in row.items()}
    handle = "migration-" + json_fingerprint(values)[:24]
    script = "\n".join((
        'require ["vacation", "date", "relational"];',
        'if allof (currentdate :zone "+0000" :value "ge" "iso8601" {},'.format(sieve_string(utc_date(start))),
        '          currentdate :zone "+0000" :value "le" "iso8601" {}) {{'.format(sieve_string(utc_date(end))),
        "  vacation :days {} :subject {} :addresses [{}]".format(days, sieve_string(row.get("subject")), sieve_string(row["email"])),
        "    :handle {} {};".format(sieve_string(handle), sieve_body(row.get("body"))), "}", "",
    ))
    return {"username": row["email"], "filter_type": "prefilter", "script_desc": "Migration vacation: " + row["email"],
            "script_data": script, "active": 0}


def vacation_assessment(
    plan: Mapping[str, Any], evidence: SieveEvidence, policy: Mapping[str, Any], now: datetime,
) -> Mapping[str, Any]:
    checks, vacations = [], []
    records = plan.get("source", {}).get("vacations")
    if not isinstance(records, list):
        return {"checks": [check("vacations", "unknown", "Source vacation inventory is missing")], "vacations": []}
    for row in records:
        identifier = "vacation:" + row["email"]
        mailbox = next((item for item in plan["mailboxes"] if item["username"] == row["email"]), None)
        start, end = vacation_timestamp(row.get("activefrom")), vacation_timestamp(row.get("activeuntil"))
        dates_utc = evidence.dates_utc or policy.get("vacationDatesAreUtc") is True
        if not active(row.get("active")) or mailbox is not None and not mailbox["active"]:
            checks.append(check(identifier, "planned", "Import supported inactive vacation settings into SOGo without enabling replies"))
        elif end is not None and end + (0 if dates_utc else 48 * 3600) < now.timestamp():
            checks.append(check(identifier, "planned", "Import supported expired vacation settings into SOGo, retaining the date window"))
        elif mailbox is None or start is None or end is None or start >= end or not dates_utc:
            checks.append(check(identifier, "decision", "Needs a real batch mailbox and valid UTC dates; verify source timezone/schema"))
        else:
            days = vacation_days(row, policy)
            script = evidence.script(row["email"])
            if not valid_vacation_days(days):
                checks.append(check(identifier, "decision",
                                    "Source interval needs a reviewed mapping; zero is not silently converted to repeated replies"))
            elif not isinstance(script, str) or re.search(r"\b(vacation|include)\b", script, re.I | re.A):
                checks.append(check(identifier, "decision", "Personal/default filter is missing or may already contain/include vacation logic"))
            else:
                try:
                    vacations.append(vacation_prefilter(row, start, end, days))
                except MigrationError as error:
                    checks.append(check(identifier, "blocked", str(error)))
                else:
                    checks.append(check(identifier, "planned",
                                        "Stage a dated Sieve prefilter; enable at batch activation; reply history starts anew"))
    if not records:
        checks.append(check("vacations", "not-needed", "No source vacation records for this batch"))
    return {"checks": checks, "vacations": vacations}


def webmail_assessment(
    plan: Mapping[str, Any], evidence: SieveEvidence, policy: Mapping[str, Any], kind: SourceKind,
    forwardings: Sequence[Mapping[str, Any]], now: Optional[datetime] = None,
) -> Mapping[str, Any]:
    checked_at = now if now is not None else datetime.now(timezone.utc)
    checks = []
    if kind is SourceKind.PLESK:
        users = [{"username": row["username"], "script": evidence.scripts.get(row["username"]), "method": "personal-sieve"}
                 for row in plan["mailboxes"]]
        checks.extend(check("personal-sieve:" + row["username"], "planned" if row["script"] is not None else "not-needed",
                            "Preserve personal Sieve; target compilation remains required" if row["script"] is not None
                            else "No personal Sieve script to copy") for row in users)
        native, vacations, prefilters = {"users": [], "blockers": []}, [], []
    else:
        converted, errors = {}, {}
        for row in plan["mailboxes"]:
            username = row["username"]
            try:
                converted[username] = mailbox_sieve(username, evidence)
            except SieveMappingError as error:
                errors[username] = str(error)
                checks.append(check("editable-filters:" + username, "blocked", str(error) + "; original script retained, conversion requires review"))
            else:
                checks.append(check("editable-filters:" + username, "planned", "{} rules mapped to editable SOGo filters".format(
                    len(converted[username]["filters"]))))
        records = plan.get("source", {}).get("vacations")
        timestamps = [value for row in (records if isinstance(records, list) else []) for field in ("activefrom", "activeuntil")
                      if (value := vacation_timestamp(row.get(field))) is not None and 0 <= value <= 2147483647]
        parts = timezone_parts(policy["vacationTimeZone"], timestamps)
        native = native_mailbox_settings(plan, evidence, policy, parts, converted, errors)
        checks.append(check("native-webmail-settings", "blocked" if native["blockers"] else "planned",
                            "\n".join(native["blockers"]) if native["blockers"] else
                            "{} users mapped to native settings; {} forwarding rules remain in a Mailcow prefilter".format(
                                len(native["users"]), sum("prefilter" in row for row in native["users"]))))
        for row in forwardings:
            prefilter = any(user["username"] == row["username"] and "prefilter" in user for user in native["users"])
            checks.append(check("mailbox-forwarding:" + row["username"], "planned",
                                "Retain the Mailcow forwarding prefilter so personal filters control the local copy" if prefilter
                                else "Map forwarding to editable SOGo Forward settings after staging"))
        assessment = vacation_assessment(plan, evidence, policy, checked_at)
        checks.extend(assessment["checks"])
        vacations = assessment["vacations"]
        prefilters = mailbox_filter_plans(vacations, forwardings)
        missing = sum(evidence.scripts.get(row["username"]) is None for row in plan["mailboxes"])
        checks.append(check("default-filtering", "not-needed" if not missing else "planned" if evidence.default_sieve is not None else "unknown",
                            "{} mailboxes without personal filters; select the exported default when installing Sieve".format(missing)))
        inactive = sieve_inventory_blockers(evidence)
        checks.append(check("inactive-sieve", "blocked" if inactive else "not-needed",
                            "\n".join(inactive) if inactive else "No inactive Sieve scripts to migrate"))
        users = [converted.get(row["username"]) or {"username": row["username"], "script": evidence.script(row["username"]),
                 "mappingError": errors[row["username"]]} for row in plan["mailboxes"]]
    scripts = [evidence.script(row["username"]) for row in plan["mailboxes"]]
    inspected = all(isinstance(script, str) and not re.search(r"\binclude\b", script, re.I | re.A) for script in scripts)
    redirects = any(isinstance(script, str) and re.search(r"\bredirect\b", script, re.I | re.A) for script in scripts)
    external = [target for row in plan["aliases"] for target in row["payload"].get("goto", "").split(",")
                if target and target.rsplit("@", 1)[-1] not in plan["domains"]]
    checks.append(check("forwarding-policy", "required" if plan["bcc"] or redirects else "not-needed" if inspected else "unknown",
                        "{} cross-domain alias destinations and {} BCC rules; {}".format(len(external), len(plan["bcc"]),
                        "ordinary aliases can be mapped; matching global routes require separate assessment" if inspected and not redirects
                        else "Sieve redirects/includes or missing filter evidence still need review")))
    checks.append(check("target-webmail", "pending",
                        "Target Sieve compilation, SOGo capability/conflict checks, timezone agreement and import verification remain pending"))
    return {"version": 1, "kind": "source-webmail-plan", "sourceKind": kind.value, "checked_at": checked_at.isoformat(),
            "target_verified": False, "checks": checks, "users": users, "nativeSettings": native,
            "vacations": vacations, "prefilters": prefilters}
