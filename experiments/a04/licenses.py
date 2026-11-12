"""Collect the effective license metadata, bundled binaries and versions of the experimental environment."""

from __future__ import annotations

import json
import platform
import sys
from importlib import metadata
from pathlib import Path

PKGS = [
    "python-docx",
    "lxml",
    "pikepdf",
    "pypdfium2",
    "pypdf",
    "Pillow",
    "fonttools",
    "typing_extensions",
    "cawbase-content-engine",
]


def main(out: str) -> None:
    import pikepdf
    import pypdfium2

    info: dict = {"python": sys.version, "platform": platform.platform(), "machine": platform.machine(), "packages": {}}
    for n in PKGS:
        d = metadata.distribution(n)
        md = d.metadata
        files = [str(f) for f in (d.files or []) if "licenses" in str(f).lower() or "LICENSE" in str(f)]
        bundled = sorted({str(f).split("/")[-1] for f in (d.files or []) if ".dylibs/" in str(f) or ".libs/" in str(f)})
        info["packages"][n] = {
            "version": d.version,
            "license": md.get("License-Expression")
            or md.get("License")
            or "; ".join(c for c in md.get_all("Classifier", []) if c.startswith("License")),
            "requires": [r for r in (d.requires or []) if "extra ==" not in r],
            "license_files": files[:40],
            "bundled_binaries_in_record": bundled,
        }
    info["libqpdf_version"] = pikepdf.__libqpdf_version__
    info["pdfium_version"] = str(pypdfium2.PDFIUM_INFO)
    info["pypdfium2_version"] = str(pypdfium2.PYPDFIUM_INFO)
    sp = Path(metadata.distribution("pypdfium2").locate_file(""))
    info["pdfium_build_license_files"] = sorted(
        p.name for p in (sp / "pypdfium2-5.15.2.dist-info/licenses/BUILD_LICENSES").glob("*") if p.is_file()
    )
    pil = metadata.distribution("Pillow")
    sbom = [f for f in (pil.files or []) if str(f).endswith(".cdx.json")]
    comps = []
    if sbom:
        for c in json.loads(Path(pil.locate_file(sbom[0])).read_text()).get("components", []):
            lic = [
                l.get("license", {}).get("id") or l.get("license", {}).get("name") or l.get("expression")
                for l in c.get("licenses", [])
            ]
            if c.get("name", "").startswith("PIL."):
                continue
            comps.append({"name": c.get("name"), "version": c.get("version"), "licenses": lic})
    info["pillow_sbom_components"] = comps
    info["pillow_bundled_libraries"] = sorted(
        {Path(str(f)).name for f in (pil.files or []) if ".dylibs/" in str(f) or ".libs/" in str(f)}
    )
    from lxml import etree

    info["lxml_libxml2"] = ".".join(map(str, etree.LIBXML_VERSION))
    info["lxml_libxslt"] = ".".join(map(str, etree.LIBXSLT_VERSION))
    pk = metadata.distribution("pikepdf")
    info["pikepdf_bundled_libraries"] = sorted(
        {Path(str(f)).name for f in (pk.files or []) if ".dylibs/" in str(f) or ".libs/" in str(f)}
    )
    info["pdfium_bundled_binary"] = sorted(
        {
            Path(str(f)).name
            for f in (metadata.distribution("pypdfium2").files or [])
            if "pdfium" in str(f) and str(f).endswith((".dylib", ".so", ".dll"))
        }
    )
    Path(out).write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: v for k, v in info.items() if k != "packages"}, indent=1))
    for n, v in info["packages"].items():
        print(f"{n} {v['version']} | {v['license']} | requires: {v['requires']}")


if __name__ == "__main__":
    main(sys.argv[1])
