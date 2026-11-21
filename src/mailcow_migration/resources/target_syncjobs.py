import json
import os
import signal
import sys

request = json.load(sys.stdin)
users = {value.encode("utf-8") for value in request["users"]}
found = []
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        with open("/proc/" + pid + "/cmdline", "rb") as stream:
            args = stream.read().split(b"\0")
        if not any(os.path.basename(arg) == b"imapsync" for arg in args[:2]):
            continue
        named = [arg.split(b"=", 1)[1] for arg in args if arg.startswith(b"--user2=")]
        named.extend(args[index + 1] for index, arg in enumerate(args[:-1]) if arg == b"--user2")
        if len(named) != 1 or not named[0]:
            raise RuntimeError("Cannot identify running imapsync owner: PID " + pid)
        mailbox, separator, master = named[0].rpartition(b"*")
        if separator and (not mailbox or not master):
            raise RuntimeError("Invalid running imapsync master login: PID " + pid)
        user = mailbox if separator else named[0]
        if user not in users:
            continue
        if request["stop"]:
            os.kill(int(pid), signal.SIGTERM)
        found.append({"pid": int(pid), "user": user.decode("utf-8")})
    except (FileNotFoundError, ProcessLookupError):
        continue
print(json.dumps(found))
