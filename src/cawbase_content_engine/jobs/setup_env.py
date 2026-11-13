"""Operator configuration of A05 from the environment (explicit call only; never at import)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from ..errors import InvalidConfigurationError

P = "CAWBASE_CONTENT_ENGINE_"


def a05_kwargs(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """``storage`` / ``documents`` keyword arguments for ``ContentEngine`` (empty when not configured)."""
    env = os.environ if environ is None else environ
    out: dict[str, Any] = {}
    root = env.get(P + "STORAGE_DIR")
    if root:
        from .setup import StorageSetup
        from .storage import FilesystemStorage

        out["storage"] = StorageSetup(managed=FilesystemStorage(root))
    iso = env.get(P + "DOCUMENTS_ISOLATION", "none").lower()
    if iso == "none":
        return out
    from .setup import DocumentSetup

    if iso == "linux_container":
        from ..documents.runner import LinuxContainerRunner

        image = env.get(P + "DOCUMENTS_IMAGE")
        if not image:
            raise InvalidConfigurationError("DOCUMENTS_IMAGE is required for the linux_container isolation.")
        out["documents"] = DocumentSetup(runner=LinuxContainerRunner(image), untrusted_input=True)
    elif iso == "unconfined_development":
        from ..documents.runner import UnconfinedDevelopmentRunner

        if env.get(P + "DOCUMENTS_UNTRUSTED_INPUT", "true").lower() != "false":
            raise InvalidConfigurationError(
                "unconfined_development needs DOCUMENTS_UNTRUSTED_INPUT=false (development with trusted input only)."
            )
        out["documents"] = DocumentSetup(runner=UnconfinedDevelopmentRunner(), untrusted_input=False)
    else:
        raise InvalidConfigurationError("DOCUMENTS_ISOLATION must be none, linux_container or unconfined_development.")
    return out
