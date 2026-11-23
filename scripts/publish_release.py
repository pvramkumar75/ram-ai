"""Publish a release that the one-line installer (get.ps1) can install.

  python scripts/publish_release.py            # build ZIP, scan, checksum, create GitHub release v<VERSION>
  python scripts/publish_release.py --dry-run  # build + checksum only

Needs the GitHub CLI (gh) signed in. Commit and push first: the release is tagged at the current commit."""
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ramai.core import VERSION  # noqa: E402


def main():
    dry = "--dry-run" in sys.argv
    # 1. build the ZIP (fails with exit 2 if personal data is found)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_share_zip.py")], check=True)
    zip_path = ROOT / "dist" / f"RAM_AI_v{VERSION}.zip"
    # 2. checksum file (format: "<HASH>  <name>", read by get.ps1)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest().upper()
    sums = ROOT / "dist" / "SHA256SUMS.txt"
    sums.write_text(f"{digest}  {zip_path.name}\n", encoding="ascii")
    print(f"SHA-256 {digest}")
    if dry:
        return
    # 3. the release must point at pushed code
    if subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip():
        sys.exit("Uncommitted changes - commit and push before publishing.")
    tag = f"v{VERSION}"
    notes = (f"RAM AI {tag}\n\nInstall or update (PowerShell):\n\n"
             "```powershell\nirm https://raw.githubusercontent.com/pvramkumar75/ram-ai/main/get.ps1 | iex\n```\n\n"
             "Or download the ZIP below, Extract All, and run Install_RAM_AI.bat.")
    subprocess.run(["gh", "release", "create", tag, str(zip_path), str(sums), "--title", f"RAM AI {tag}",
                    "--notes", notes, "--target", "main"], cwd=ROOT, check=True)
    print(f"Published {tag}")


if __name__ == "__main__":
    main()
