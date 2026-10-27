from typing import Any, Mapping

from .assessment import check
from .domain import address


def password_expiration_check(policy: Mapping[str, Any]) -> Mapping[str, str]:
    expiration = policy["sourcePasswordExpiration"]
    return check(
        "password-expiration",
        "not-needed" if expiration == "disabled" else "blocked",
        "Source password expiration is disabled under the existing policy"
        if expiration == "disabled"
        else "Mailcow has no native dated/recurring password expiration; force_pw_update is not equivalent"
        if expiration == "enabled"
        else "Confirm whether the source actually enforces password expiration; database dates do not establish this",
    )


def recovery_plan(plan: Mapping[str, Any]) -> Mapping[str, Any]:
    records = plan.get("source", {}).get("mailboxes")
    if not isinstance(records, list):
        return {
            "emails": [],
            "checks": [
                check(
                    "recovery-contacts",
                    "blocked",
                    "Source recovery-contact inventory is missing",
                )
            ],
        }
    emails, checks = [], []
    for row in records:
        username, email, phone = (
            row["username"],
            str(row.get("email_other") or "").strip(),
            str(row.get("phone") or "").strip(),
        )
        if phone:
            checks.append(
                check(
                    "recovery:" + username,
                    "blocked",
                    "Mailcow supports recovery email, not phone/SMS recovery",
                )
            )
        if email and (not address(email.lower()) or email.lower() == username.lower()):
            checks.append(
                check(
                    "recovery:" + username,
                    "blocked",
                    "Invalid or self-referencing recovery email; correct the source value",
                )
            )
        if (email or phone) and not any(
            item["username"] == username for item in plan["mailboxes"]
        ):
            checks.append(
                check(
                    "recovery:" + username,
                    "blocked",
                    "Recovery contact has no target mailbox",
                )
            )
        if email:
            emails.append(
                {
                    "username": username,
                    "email": email,
                    "operation": "import recovery email " + username,
                }
            )
    return {"emails": emails, "checks": checks}
