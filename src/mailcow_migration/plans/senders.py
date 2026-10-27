import re
from typing import Any, Mapping, Optional, Sequence, Tuple

from ..core.postfix import logical_lines, postfix_settings
from .postfix import PostfixEvidence

OWNERSHIP_RULES = frozenset(
    (
        "reject_sender_login_mismatch",
        "reject_authenticated_sender_login_mismatch",
        "reject_known_sender_login_mismatch",
    )
)
INDEPENDENT_RULES = frozenset(
    (
        "permit_mynetworks",
        "reject",
        "defer",
        "defer_if_permit",
        "defer_if_reject",
        "reject_unauth_destination",
        "defer_unauth_destination",
        "reject_unauth_pipelining",
        "reject_multi_recipient_bounce",
        "reject_plaintext_session",
        "reject_non_fqdn_sender",
        "reject_non_fqdn_recipient",
        "reject_non_fqdn_helo_hostname",
        "reject_invalid_helo_hostname",
        "reject_unknown_sender_domain",
        "reject_unknown_recipient_domain",
        "reject_unknown_helo_hostname",
        "reject_unknown_client_hostname",
        "reject_unknown_reverse_client_hostname",
        "reject_unlisted_sender",
        "reject_unlisted_recipient",
        "reject_unauthenticated_sender_login_mismatch",
        "permit_auth_destination",
        "permit_inet_interfaces",
        "sleep",
    )
)
RESTRICTION_KEYS = tuple(
    f"smtpd_{stage}_restrictions"
    for stage in (
        "client",
        "helo",
        "sender",
        "relay",
        "recipient",
        "data",
        "end_of_data",
    )
)
CLEANUP_KEYS = (
    "header_checks",
    "mime_header_checks",
    "nested_header_checks",
    "body_checks",
)
EXTERNAL_KEYS = (
    "smtpd_milters",
    "smtpd_milter_maps",
    "content_filter",
    "smtpd_proxy_filter",
    "smtpd_command_filter",
    "smtpd_end_of_data_command_filter",
)


def expand_setting(
    config: Mapping[str, str], key: str, stack: Tuple[str, ...] = ()
) -> str:
    if key in stack:
        return "$" + key
    return re.sub(
        r"\$\{([a-z][a-z0-9_]*)\}|\$([a-z][a-z0-9_]*)",
        lambda match: (
            expand_setting(config, match[1] or match[2], (*stack, key))
            if (match[1] or match[2]) in config
            else match[0]
        ),
        config.get(key) or "",
    )


def postfix_services(
    text: Optional[str], defaults: Mapping[str, str]
) -> Tuple[Mapping[str, Any], ...]:
    options = re.compile(
        r"(?:^|\s)-o\s+(?:\{\s*([a-z][a-z0-9_]*)\s*=\s*([^{}]*?)\s*\}|"
        r"([a-z][a-z0-9_]*)=([^\s{}]*))(?=\s|$)"
    )
    services = []
    for line in logical_lines(text):
        match = re.fullmatch(
            r"(\S+)\s+(inet|unix|pass)\s+(?:\S+\s+){5}(?:\S*/)?(smtpd|cleanup)(?:\s+(.*))?",
            line,
        )
        if match is None:
            continue
        overrides = {
            option[1] or option[3]: option[2] if option[2] is not None else option[4]
            for option in options.finditer(match[4] or "")
        }
        remaining = options.sub(" ", match[4] or "")
        services.append(
            {
                "service": match[1] + "/" + match[2],
                "command": match[3],
                "config": {**defaults, **overrides},
                "unsupported": re.sub(
                    r"(?:^|\s)-(?:v|D)(?=\s|$)", " ", remaining
                ).strip(),
            }
        )
    return tuple(services)


def inspect_service(
    service: Mapping[str, Any], services: Sequence[Mapping[str, Any]]
) -> Mapping[str, str]:
    config = service["config"]

    def result(state: str, detail: str) -> Mapping[str, str]:
        return {"service": service["service"], "state": state, "detail": detail}

    if service["unsupported"]:
        return result(
            "unknown",
            "SMTP service options could not be fully parsed in postfix-services.conf",
        )
    authentication = expand_setting(config, "smtpd_sasl_auth_enable") or "no"
    if authentication == "no":
        return result(
            "disabled",
            "SMTP authentication disabled; trusted-IP sending needs separate review",
        )
    if authentication != "yes":
        return result("unknown", "smtpd_sasl_auth_enable could not be resolved")
    chains = {key: expand_setting(config, key) for key in RESTRICTION_KEYS}
    configured = [
        f"{key}: {rule}"
        for key, value in chains.items()
        for rule in re.split(r"[\s,]+", value)
        if rule in OWNERSHIP_RULES
    ]
    maps = expand_setting(config, "smtpd_sender_login_maps")
    if configured:
        return result(
            "rules-found",
            "{}. Sender-owner lookup: {}. ".format(
                "; ".join(configured), maps or "empty"
            )
            + "Lookup results, restriction order, warn_if_reject and soft_bounce must be reviewed before deriving grants; "
            "configured rules alone do not prove enforcement for every login.",
        )
    unresolved = []
    for key, value in chains.items():
        tokens = [token for token in re.split(r"[\s,]+", value) if token]
        end = next(
            (
                index
                for index, token in enumerate(tokens)
                if token in ("permit_sasl_authenticated", "permit")
            ),
            len(tokens),
        )
        relevant = tokens[:end]
        if any(
            token not in INDEPENDENT_RULES
            and token != "warn_if_reject"
            and not (
                index > 0
                and relevant[index - 1] == "sleep"
                and re.fullmatch(r"[0-9]+", token)
            )
            for index, token in enumerate(relevant)
        ):
            unresolved.append(key)
    external = [key for key in EXTERNAL_KEYS if expand_setting(config, key)]
    cleanup_name = (
        expand_setting(config, "cleanup_service_name") or "cleanup"
    ) + "/unix"
    cleanup = next(
        (
            row
            for row in services
            if row["service"] == cleanup_name and row["command"] == "cleanup"
        ),
        None,
    )
    cleanup_dependencies = (
        [cleanup_name + " service configuration"]
        if cleanup is None or cleanup["unsupported"]
        else [
            cleanup_name + ": " + key
            for key in CLEANUP_KEYS
            if expand_setting(cleanup["config"], key)
        ]
    )
    dependencies = [
        *unresolved,
        *external,
        *cleanup_dependencies,
        *(
            ["smtpd_restriction_classes"]
            if expand_setting(config, "smtpd_restriction_classes")
            else []
        ),
    ]
    if dependencies:
        return result(
            "unknown",
            "No direct authenticated sender-login restriction found, but "
            + ", ".join(dependencies)
            + " require inspection of referenced classes, lookups or filters.",
        )
    return result(
        "no-ownership-check",
        "No sender/login ownership restriction in the inspected authenticated SMTP path. "
        + (
            f"smtpd_sender_login_maps ({maps}) is configured without an enforcing restriction. "
            if maps
            else ""
        )
        + "Other delivery checks still apply; there is no enforced per-user sender list to import from this path.",
    )


def sender_permission_check(
    evidence: PostfixEvidence, policy: Mapping[str, Any]
) -> Mapping[str, Any]:
    count = sum(
        len(senders) for senders in policy.get("senderPermissions", {}).values()
    )
    policy_detail = (
        "{} extra sender permission{} configured for import in the settings policy.".format(
            count, "" if count == 1 else "s"
        )
        if count
        else "No extra sender permissions will be added.\n  Add explicit permissions to the settings policy if needed."
    )

    def finish(
        status: str, state: str, detail: str, services: Sequence[Mapping[str, str]] = ()
    ) -> Mapping[str, Any]:
        return {
            "id": "sender-permissions",
            "status": status,
            "sourceState": state,
            "services": list(services),
            "detail": detail + "\n  " + policy_detail,
        }

    missing = [
        file
        for file, value in (
            ("postfix-effective.conf", evidence.postfix),
            ("postfix-services.conf", evidence.services),
        )
        if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        return finish(
            "unknown",
            "missing-evidence",
            "Cannot inspect source sender rules: missing {}.".format(
                ", ".join(missing)
            ),
        )
    defaults = postfix_settings("\n".join(logical_lines(evidence.postfix)))
    if not defaults:
        return finish(
            "unknown",
            "missing-evidence",
            "No Postfix settings could be read from the export.",
        )
    configured = postfix_services(evidence.services, defaults)
    services = [
        inspect_service(row, configured)
        for row in configured
        if row["command"] == "smtpd"
    ]
    if not services:
        return finish(
            "unknown",
            "missing-evidence",
            "No supported SMTP service found in postfix-services.conf.",
        )
    authenticated = [row for row in services if row["state"] != "disabled"]
    state = (
        "authentication-disabled"
        if not authenticated
        else "unknown"
        if any(row["state"] == "unknown" for row in authenticated)
        else "rules-found"
        if any(row["state"] == "rules-found" for row in authenticated)
        else "no-ownership-check"
    )
    detail = {
        "authentication-disabled": "SMTP login is disabled on the inspected services. Review permissions for applications using trusted IPs.",
        "unknown": "Some sender rules could not be verified. Review the per-service findings in the saved assessment.",
        "rules-found": "Sender restrictions were found. Review their lookup tables before importing permissions.",
        "no-ownership-check": "No per-user sender restrictions found in the checked server settings.",
    }[state]
    return finish(
        "unknown" if state == "unknown" else "required", state, detail, services
    )
