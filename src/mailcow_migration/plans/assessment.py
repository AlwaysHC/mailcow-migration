from typing import Mapping


def check(identifier: str, status: str, detail: str) -> Mapping[str, str]:
    return {"id": identifier, "status": status, "detail": detail}
