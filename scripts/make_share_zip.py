"""Build the shareable package: dist/RAM_AI_v<version>.zip  (code + skills + installer + guides; no personal data).

Excludes: .venv, tests, dev scripts, caches, BUILD_STATUS (your test notes). Personal data never lives in this folder anyway
(it is in %APPDATA%\\RAM_AI on each PC). The script also scans the package for leftover personal identifiers."""
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ramai.core import VERSION  # noqa: E402

INCLUDE = ["ramai", "docs", "README.md", "HANDOVER.md", "requirements.txt", "requirements-workspace.txt",
           "Install_RAM_AI.bat", "Install_RAM_AI.ps1", "Uninstall_RAM_AI.bat", "Uninstall_RAM_AI.ps1", "Start_RAM_AI.bat", ".env.example"]
SKIP_PARTS = {"__pycache__", ".venv", ".pytest_cache"}
def _personal_pattern():
    """Words that must never ship (your name, emails, company...). Kept OUTSIDE the repo in %APPDATA%\\RAM_AI\\share_scan_terms.txt
    (one term per line) so the list itself is never published. Your Windows profile path is always checked."""
    import os

    terms = []
    f = os.path.join(os.environ.get("APPDATA", ""), "RAM_AI", "share_scan_terms.txt")
    if os.path.exists(f):
        terms = [t.strip() for t in open(f, encoding="utf-8") if t.strip() and not t.startswith("#")]
    terms = [re.escape(t) for t in terms] + [re.escape(os.path.expanduser("~"))]
    return re.compile("|".join(terms), re.I)


PERSONAL = _personal_pattern()


def files():
    for item in INCLUDE:
        p = ROOT / item
        if p.is_file():
            yield p
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and not (SKIP_PARTS & set(f.parts)) and f.suffix not in (".pyc",):
                    yield f


def main():
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / f"RAM_AI_v{VERSION}.zip"
    leaks = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files():
            rel = f.relative_to(ROOT)
            if f.suffix in (".py", ".md", ".js", ".html", ".css", ".txt", ".ps1", ".bat", ".example"):
                txt = f.read_text(encoding="utf-8", errors="replace")
                for m in PERSONAL.finditer(txt):
                    leaks.append(f"{rel}: …{txt[max(0, m.start() - 30):m.end() + 30]!r}…")
            z.write(f, Path("RAM_AI") / rel)
    print(f"built {out}  ({out.stat().st_size // 1024} KB, {len(zipfile.ZipFile(out).namelist())} files)")
    if leaks:
        print("PERSONAL DATA FOUND - not ready to share:\n  " + "\n  ".join(leaks[:20]))
        sys.exit(2)
    print("personal-data scan: clean")


if __name__ == "__main__":
    main()
