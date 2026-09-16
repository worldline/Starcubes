"""Who this process is, across containers.

A PID names a process only inside its own PID namespace, and a hostname names a
container only until a StatefulSet restarts it under the same name with a fresh
process table. So every starcubes process also carries an *instance id*: one
value per container start, inherited by everything that container spawns.

`docker/entrypoint.sh` generates it and exports `STARCUBES_INSTANCE_ID` before
anything else runs. Outside the image — `runserver`, a shell — the first call to
`instance_id()` generates one and writes it back into `os.environ`, so a child
spawned afterwards inherits the same value. `starcubes/settings.py` makes that
first call at import time, before uwsgi forks its workers or any management
command spawns a child.

No Django imports — same constraint as the rest of `core/`.
"""
from __future__ import annotations

import os
import socket
import uuid

ENV_VAR = "STARCUBES_INSTANCE_ID"


def instance_id() -> str:
    """This container's instance id, generating and exporting one if unset."""
    value = os.environ.get(ENV_VAR)
    if not value:
        value = str(uuid.uuid4())
        os.environ[ENV_VAR] = value
    return value


def short_instance_id() -> str:
    """The first eight hex digits — enough to tell containers apart on screen."""
    return instance_id().replace("-", "")[:8]


def hostname() -> str:
    """The host name as the container sees it (the pod name under k8s)."""
    return socket.gethostname()
