"""Build the tree that can be published, and refuse to build one that cannot.

    .venv-pilot/bin/python tools/publish_export.py --out /tmp/publish
    .venv-pilot/bin/python tools/publish_export.py --out /tmp/publish --report

Why a tool instead of "clone it and delete the private bits": this working
directory is where the project *lives*, and it contains things that must never
leave it -- the operator's mailbox address, real students' addresses in a design
mock, the production host, and a diary (`HANDOVER.md`, `AGENTS.md`) whose whole
value is that it is candid. A manual copy is a decision made once, by hand, at
the end of a long day. This is the same decision written down and re-runnable.

The policy is deliberately **default-deny**: everything published is named in
``INCLUDE`` below, so a new file in this directory is not published until someone
adds it here on purpose. Two independent gates then run over the result:

1. ``handoff.scan_secrets`` -- the project's existing credential scanner, reused
   verbatim so "what looks like a credential" has exactly one definition;
2. a *personal-data* scan (`_scan_private`) -- the failure this repository is
   actually exposed to. Nothing here is a leaked API key; what would hurt is the
   operator's address, a real student's address, the production host, or the
   master-key fingerprint.

Either gate finding anything means exit code 3 and no output directory, because
"publish anyway and clean up later" is not available: a git push cannot be
recalled. See ``docs/publishing-2026-09-15.md`` for the decision record.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pilot_app import credentials  # noqa: E402

# The marker that lets a file declare itself a fixture. Defined here rather than
# imported from tools/handoff.py, which is not published: the publication tool
# must be able to run from the tree it just produced.
SNAPSHOT_SCAN_EXEMPT = re.compile(r"^handoff-security-scan: fixtures\s*$", re.MULTILINE)

# ---------------------------------------------------------------------------
# what goes out
# ---------------------------------------------------------------------------
# Whole directories, minus the names in EXCLUDE_NAMES / EXCLUDE_SUFFIXES.
INCLUDE_DIRS = (
    "pilot_app",
    "tools",
)

# Single files.
INCLUDE_FILES = (
    "LICENSE",
    "README.md",
)

# Documentation is the part most likely to carry somebody's address, so it is an
# allowlist too -- and a short one. These are the pages that help a stranger run
# this thing; the rest are the project's diary.
INCLUDE_DOCS = (
    "docs/backup-2026-09-14.md",
    "docs/compliance-2026-09-14.md",
    "docs/platform-key-2026-09-14.md",
    "docs/restore-drill.md",
    "docs/agent-monitor-2026-09-15.md",
    "docs/agent-handoff-2026-09-14.md",
    "docs/email-html-compatibility-2026-09-13.md",
    "docs/open-source-recon-2026-09-14.md",
    # The README points here for "why not an app store?". It is self-contained
    # (it references only two published files) and a self-hoster faces the same
    # question -- whereas a README that links a page we deliberately withhold is
    # a dead link on the front door, which is the thing this allowlist exists to
    # avoid in the first place.
    "docs/app-distribution-decision-2026-09-14.md",
    "docs/selfhost-distribution-recon-2026-09-14.md",
    "docs/python-distribution-recon-2026-09-14.md",
    # How a self-hoster's account stops burning generation slots on a key the
    # provider keeps refusing. Added together with the feature (v0.63.4), because
    # this allowlist deliberately does not publish new files by default -- and
    # this is the step that would otherwise be forgotten, leaving the code public
    # and the reasoning private.
    "docs/key-circuit-2026-09-15.md",
)

# Never published, whatever else says otherwise. Each line is a reason.
EXCLUDE_NAMES = {
    "__pycache__",
    ".DS_Store",
    ".secrets",          # QQ app password handed over for diagnostics
    "handoff",           # cross-agent ledger + full source snapshots
    "publish-private.json",  # the scrub rules themselves are the private data
    "dist",              # release tarballs (the reader builds their own)
    "work",
    "outputs",
    "cloud_deploy",
    "outlook_ai_assistant",
    ".venv-pilot",
    ".e2e",
    ".tools",
    "preview",
    # Tools that are about *this* installation rather than about the software.
    "handoff.py",              # reads AGENTS.md / HANDOVER.md, which are not published
    "test_handoff.py",         # tests that tool, so it cannot run without it
    "post_first_notice.py",    # hardcodes the operator's address
    "make_design_options.py",  # HTML mocks built from real pilot rows
}

EXCLUDE_SUFFIXES = (".pyc", ".sqlite3", ".sqlite3-shm", ".sqlite3-wal")

# The .gitignore that the published repository should have. Written by this tool
# rather than copied, because the local one knows about local-only paths.
PUBLIC_GITIGNORE = """\
# Local-only. Kept out of the repository on purpose: the master key never leaves
# the server's 0600 environment file, and a database with real mail in it does
# not belong in a public repository either.
.secrets/
pilot.env
*.env
*.sqlite3
*.sqlite3-shm
*.sqlite3-wal
.venv/
.venv-pilot/
__pycache__/
*.pyc
dist/
preview/
.DS_Store
"""

# ---------------------------------------------------------------------------
# scrubbing
# ---------------------------------------------------------------------------
# Applied in order, so a specific rule must come before a general one (the
# operator's address is also a QQ address).
#
# Note what is NOT here. The first version of this file listed the production IP
# and the operator's address as literals, which meant the deny-list itself was
# the leak: publishing the tool published exactly the two strings it exists to
# remove. Values specific to *this* installation therefore live in
# `publish-private.json`, which is never published, and only generic rules --
# shapes that are personal wherever they appear -- are written down here.
GENERIC_SCRUBS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\b[A-Za-z0-9._%+-]+@my\.cityu\.edu\.hk\b"),
     "student@my.cityu.edu.hk", "真实用户邮箱（CityU）"),
)

PRIVATE_RULES_PATH = ROOT / "publish-private.json"


def load_forbidden() -> list[str]:
    """Identifiers that must not appear anywhere in a published file.

    The scrub rules handle *shapes* (an address at this domain, this host, this
    key name). This handles the case that got past them the first time: a bare
    account name with no address around it. The list lives in the unpublished
    private file for the obvious reason -- the names are the thing being hidden.
    """
    import json

    if not PRIVATE_RULES_PATH.is_file():
        return []
    try:
        payload = json.loads(PRIVATE_RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(item.get("value", "")) for item in payload.get("forbidden", [])
            if item.get("value")]


def load_private_rules() -> list[tuple[re.Pattern[str], str, str]]:
    """Installation-specific replacements, from a file that is never published.

    Missing file is not an error: the export still runs, with only the generic
    rules, and the verifier below is what decides whether the result is safe. A
    copy of this project that someone else downloads has no private rules and
    needs none.
    """
    import json

    if not PRIVATE_RULES_PATH.is_file():
        return []
    try:
        payload = json.loads(PRIVATE_RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"警告：{PRIVATE_RULES_PATH.name} 读不出来（{error}），只用通用规则。", file=sys.stderr)
        return []
    rules = []
    for entry in payload.get("rules", []):
        try:
            rules.append((re.compile(entry["pattern"]), entry["replace"], entry["label"]))
        except (KeyError, re.error) as error:
            print(f"警告：跳过一条无效规则（{error}）", file=sys.stderr)
    return rules

# Publication gate for addresses: "provably made up", checked on two axes.
#
# The first run of this tool produced the complete inventory (76 distinct
# addresses, 195 occurrences); reviewing it showed every one is either a fixture
# this project's tests invented or a public corporate role address that its
# sample mail quotes. Rather than paste 76 lines, the two things that actually
# distinguish a fixture are written down: a *stand-in local part* and a *domain
# that is reserved, a test provider, or quoted sample mail*. Anything outside
# both lists fails the export, so a real address arriving in the tree stops the
# build until a person looks at it -- which is the property worth having.
FIXTURE_LOCAL_PARTS = frozenset("""
a b c d e p s t t1 u v x y me box box1 pilot report reports old other owner purge new
one two someone you student teacher library lib career fees finance reg hello getstarted
no-reply noreply noreply_cap275421 account-security-noreply attacker operator mixed boss
privacy promo secret app-pass-123 shared-forward demo-cityu-1 demo-personal-1 10000
cityu-mail-pilot-alert
""".split())

# Domains that are proof on their own: nobody has a mailbox at any of these, so
# the local part may be anything ("member-a@example.com" is still made up).
RESERVED_DOMAINS = (
    r"^(?:[a-z0-9-]+\.)*(?:example(?:\.(?:com|org|net|edu|co))?|invalid|test|localhost)$",
    r"^[a-z]$|^[a-z]\.(?:com|hk|org|net)$",      # "a@b.com", "t@x.hk"
    r"^[a-z0-9.-]*\.service$",                    # a unit template, not a mailbox
    # GitHub 给不想暴露真实邮箱的人生成的地址。它本来就是匿名形式，而且这个仓库里
    # 出现的是**默认占位值**，不是任何人的邮箱。
    r"^users\.noreply\.github\.com$",
)

# Domains that exist in the real world, so the local part has to be a stand-in
# before the address counts as a fixture.
SAFE_DOMAINS = (
    r"^(?:qq|163|gmail|outlook|icloud|yahoo|yeah|foxmail|q)\.(?:com|com\.hk|net)$",
    r"^(?:[a-z0-9-]+\.)*cityu\.edu\.hk$",
    r"^smtp\d+\.ad\.cityu\.edu\.hk$",
    r"^notcityu\.edu\.hk$",                       # anti-spoofing test look-alikes
    r"^[a-z0-9.-]*cityu\.edu\.hk\.evil\.com$",
    r"^(?:mail\.grammarly\.com|codefinity\.com|fairwood\.com\.hk|accountprotection\.microsoft\.com)$",
    r"^other\.edu$",
)

# "20260913091828.5982EBAE32@smtp82.ad.cityu.edu.hk" -- a fixture Message-ID, and
# the local part is a timestamp plus a hex run rather than anybody's name.
MESSAGE_ID_LOCAL = re.compile(r"^\d{10,}\.[0-9A-Fa-f]{6,}$")


def _address_is_safe(address: str) -> bool:
    local, _, domain = address.rpartition("@")
    if not local or not domain:
        return False
    # `git@github.com` 是 SSH 远程地址，不是邮箱：局部名 `git` 是全世界 VCS 都用
    # 的那个系统账号。正则分不出这两者，所以在这里排除。
    if local in {"git", "hg", "svn"}:
        return True
    domain = domain.lower()
    if any(re.fullmatch(pattern, domain) for pattern in RESERVED_DOMAINS):
        return True
    if not any(re.fullmatch(pattern, domain) for pattern in SAFE_DOMAINS):
        return False
    if MESSAGE_ID_LOCAL.match(local):
        return True
    return local.lower() in FIXTURE_LOCAL_PARTS or len(local) <= 3
ADDRESS = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Only *this* machine's home directory is a leak. `/home/zulip` in a research note
# is another project's documented path, and flagging it would teach the reader to
# skim past this check.
PRIVATE_HOME = re.compile(re.escape(str(Path.home().parent / Path.home().name)))

# Ranges that are safe to publish, with the reason each one is safe.
SAFE_IP_PREFIXES = (
    "127.0.0.1",       # loopback
    "192.0.2.",        # RFC 5737 documentation
    "198.51.100.",     # RFC 5737 documentation
    "203.0.113.",      # RFC 5737 documentation (also the placeholder above)
    "10.",             # RFC 1918, not routable
    "192.168.",        # RFC 1918
)
SAFE_IP_172 = re.compile(r"^172\.(1[6-9]|2\d|3[01])\.")


def _scrub(text: str, rules) -> tuple[str, list[str]]:
    """Return the text with private values replaced, and what was replaced."""
    hits: list[str] = []
    for pattern, replacement, label in rules:
        text, count = pattern.subn(replacement, text)
        if count:
            hits.extend([label] * count)
    return text, hits


def _scan_private(text: str) -> list[str]:
    """Personal data and infrastructure that survived scrubbing.

    Deliberately checks the *result*, not the input: a scrubber that quietly
    stops matching (because someone edits the source text) must fail loudly here
    rather than ship.
    """
    problems: list[str] = []
    for match in ADDRESS.finditer(text):
        if not _address_is_safe(match.group(0)):
            problems.append(f"未替换的邮箱地址：{match.group(0)}（若确属虚构夹具，"
                            f"加进 SAFE_ADDRESS_PATTERNS 并说明理由）")
    for match in IPV4.finditer(text):
        octets = [int(part) for part in match.group(0).split(".")]
        if any(part > 255 for part in octets):
            continue
        value = match.group(0)
        if value.startswith(SAFE_IP_PREFIXES) or SAFE_IP_172.match(value):
            continue
        problems.append(f"未替换的公网 IP：{value}")
    for match in PRIVATE_HOME.finditer(text):
        problems.append(f"本机绝对路径：{match.group(0)}")
    return problems


def iter_files() -> list[Path]:
    """Every file the policy selects, relative to the repository root."""
    chosen: list[Path] = []
    for name in INCLUDE_DIRS:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDE_NAMES for part in relative.parts):
                continue
            if relative.name in EXCLUDE_NAMES:
                continue
            if path.name.endswith(EXCLUDE_SUFFIXES):
                continue
            chosen.append(relative)
    for name in INCLUDE_FILES + INCLUDE_DOCS:
        path = ROOT / name
        if path.is_file():
            chosen.append(Path(name))
    return sorted(set(chosen))


def build(out_dir: Path, rules, forbidden: list[str] | None = None) -> int:
    files = iter_files()
    forbidden = forbidden or []
    scrubbed: dict[str, int] = {}
    problems: list[str] = []
    exempted: list[str] = []
    written: list[tuple[str, str]] = []

    for relative in files:
        source = ROOT / relative
        try:
            text = source.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Binary assets (the PNGs under static/) travel unchanged; they are
            # small, hand-generated by tools/make_icons.py, and carry no EXIF.
            blob = source.read_bytes()
            written.append((str(relative), hashlib.sha256(blob).hexdigest()))
            target = out_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            continue

        cleaned, hits = _scrub(text, rules)
        for label in hits:
            scrubbed[label] = scrubbed.get(label, 0) + 1
        # Source mode: the same call the offline snapshot makes. It drops the
        # patterns that exist for prose and logs but match ordinary code
        # (`password = secrets.decrypt(...)`), and it refuses to let the docs
        # vouch for a value the docs themselves contain.
        if SNAPSHOT_SCAN_EXEMPT.search(cleaned):
            # The project's existing escape hatch, with the project's rule: the
            # declaration has to be on a line of its own, and the file is named
            # in the output so a reader can see what was waved through.
            exempted.append(str(relative))
        else:
            for problem in credentials.scan_secrets(cleaned, source=True):
                problems.append(f"{relative}: {problem}")
        for problem in _scan_private(cleaned):
            problems.append(f"{relative}: {problem}")
        for token in forbidden:
            if token in cleaned:
                problems.append(f"{relative}: 出现了禁止公开的标识符（{len(token)} 字符，"
                                f"见 publish-private.json）")

        target = out_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(cleaned, encoding="utf-8")
        written.append((str(relative), hashlib.sha256(cleaned.encode("utf-8")).hexdigest()))

    (out_dir / ".gitignore").write_text(PUBLIC_GITIGNORE, encoding="utf-8")
    written.append((".gitignore", hashlib.sha256(PUBLIC_GITIGNORE.encode("utf-8")).hexdigest()))

    # Pure hash lines, so `shasum -a 256 -c PUBLISH-MANIFEST.txt` is silent and
    # therefore actually useful: a tool that prints warnings every time is a tool
    # whose output nobody reads. The prose lives in its own file.
    manifest = [f"{digest}  {name}" for name, digest in sorted(written)]
    (out_dir / "PUBLISH-MANIFEST.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    (out_dir / "PUBLISH-NOTES.txt").write_text(
        "这个包由 tools/publish_export.py 生成。\n"
        f"文件数：{len(written)}\n\n"
        "自证完整性：shasum -a 256 -c PUBLISH-MANIFEST.txt\n"
        "私有信息（生产域名/IP、运营者与用户的邮箱、部署密钥名、主密钥指纹）\n"
        "在导出时已被替换成占位值；替换规则不在这个包里。\n", encoding="utf-8")

    if problems:
        print("拒绝导出——下面这些内容不该出现在公开仓库里：", file=sys.stderr)
        for problem in sorted(set(problems)):
            print(f"  {problem}", file=sys.stderr)
        print("\n修掉它们（或调整 SCRUBS / INCLUDE），再跑一次。", file=sys.stderr)
        return 3

    print(f"文件数：{len(written)}")
    if exempted:
        print("按文件内声明跳过了凭据扫描：")
        for name in sorted(exempted):
            print(f"  {name}")
    if scrubbed:
        print("替换掉的私有信息：")
        for label, count in sorted(scrubbed.items()):
            print(f"  {label}: {count} 处")
    else:
        print("没有需要替换的私有信息（这本身可能值得怀疑——核对一下）。")
    print("清单：PUBLISH-MANIFEST.txt（`shasum -a 256 -c` 可自证）")
    return 0


def report() -> int:
    """Print what the policy selects, without writing anything."""
    files = iter_files()
    print(f"会公开 {len(files)} 个文件：")
    for relative in files:
        print(f"  {relative}")
    for name, reason in sorted({name: "本地/私密" for name in EXCLUDE_NAMES}.items()):
        if (ROOT / name).exists():
            print(f"排除：{name}（{reason}）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成可以公开的源码树（默认拒绝）")
    parser.add_argument("--out", default="", help="输出目录；省略时只打印清单")
    parser.add_argument("--report", action="store_true", help="只打印会公开哪些文件")
    parser.add_argument("--force", action="store_true", help="输出目录已存在时也覆盖")
    args = parser.parse_args(argv)

    if args.report or not args.out:
        return report()

    out_dir = Path(args.out).expanduser().resolve()
    if out_dir.exists() and not args.force:
        print(f"{out_dir} 已存在；加 --force 覆盖，或换一个目录。", file=sys.stderr)
        return 2

    # Build somewhere else and move it into place only on success. A refused
    # export must not leave a directory that looks ready to publish: the whole
    # point of the gate is that the easy next step is not available.
    staging = out_dir.parent / (out_dir.name + ".building")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        code = build(staging, GENERIC_SCRUBS + tuple(load_private_rules()), load_forbidden())
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if code != 0:
        shutil.rmtree(staging, ignore_errors=True)
        return code
    if out_dir.exists():
        shutil.rmtree(out_dir)
    staging.rename(out_dir)
    print(f"已生成 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
