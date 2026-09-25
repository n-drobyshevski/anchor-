"""app/web/static/vendor/ hygiene (W1 plan step 1, scripts/vendor_web.py).

- every vendored file's sha256 matches VENDOR.lock
- the set of files in vendor/ and vendor/fonts/ (besides VENDOR.lock
  itself) equals the set VENDOR.lock records
- no bare `from"pkg"`/`from "pkg"`/`import"pkg"`/`import "pkg"`
  specifier remains in any vendored JS module
- no `eval(` or `new Function` in any vendored JS module
- every vendored font is a real WOFF2 under the static size cap
- VENDOR.lock's own integrity values match scripts/vendor_web.py's
  committed pins -- not merely internally self-consistent

The vendor files this module checks are produced by a real run of
scripts/vendor_web.py against the real npm registry and are expected to
already be on disk in this repo (this module's own job is checking
them, not producing them). `_require_lock` fails hard, rather than
skipping, when VENDOR.lock is missing (low-severity finding: the old
`pytest.skip` here meant a run of vendor_web.py that failed partway and
left VENDOR.lock deleted -- see that script's own `main()` docstring on
why it no longer can -- silently skipped every test in this module
instead of failing the one that most needed to catch it).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from scripts.vendor_web import _FONT_PACKAGES, _PACKAGES, _PINNED_INTEGRITY

VENDOR_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "vendor"
LOCK_PATH = VENDOR_DIR / "VENDOR.lock"

_FORBIDDEN_SNIPPETS = ("eval(", "new Function")

# Same shape as scripts/vendor_web.py's own _IMPORT_RE (that script's
# `from"pkg"`/`from "pkg"`/`import"pkg"`/`import "pkg"` rewriter), used
# here instead of this test's own hand-rolled marker list -- see
# test_no_bare_specifiers_remain_in_any_vendored_file's docstring for
# why the marker list used to miss real cases this regex does not.
_IMPORT_RE = re.compile(r'\b(?:from|import)\s*\(?\s*(["\'])([^"\']+)\1')


_FONT_NAME_RE = re.compile(r"fonts/[a-z0-9-]+\.woff2")


def _module_names(lock: dict) -> list[str]:
    """The locked JS modules only. The self-hosted fonts under
    `fonts/` are binary: they get the sha256 and pin checks like every
    other file, but the text checks (bare specifiers, eval, source
    maps) only make sense for JS."""
    return [name for name in lock["files"] if name.endswith(".module.js")]


def _require_lock() -> dict:
    assert LOCK_PATH.is_file(), f"{LOCK_PATH} does not exist -- run scripts/vendor_web.py"
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def test_vendor_lock_is_valid_json_with_the_expected_shape():
    lock = _require_lock()
    assert set(lock.keys()) == {"packages", "files"}
    assert lock["files"], "VENDOR.lock lists no files"
    for name, entry in lock["files"].items():
        assert isinstance(name, str)
        assert name.endswith(".module.js") or _FONT_NAME_RE.fullmatch(name), name
        if name.endswith(".module.js"):
            assert "/" not in name, name
        assert set(entry.keys()) == {"package", "sha256"}
        assert entry["package"] in lock["packages"]
    for name, entry in lock["packages"].items():
        assert set(entry.keys()) == {"version", "integrity"}
        assert entry["integrity"].startswith("sha512-")


def test_every_locked_file_matches_its_recorded_sha256():
    lock = _require_lock()
    for name, entry in lock["files"].items():
        path = VENDOR_DIR / name
        assert path.is_file(), f"{name} is listed in VENDOR.lock but missing on disk"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == entry["sha256"], f"{name}: sha256 does not match VENDOR.lock"


def test_vendor_directory_contains_exactly_the_locked_files():
    lock = _require_lock()
    on_disk = {
        p.relative_to(VENDOR_DIR).as_posix()
        for p in VENDOR_DIR.rglob("*")
        if p.is_file() and p != LOCK_PATH
    }
    assert on_disk == set(lock["files"])


def test_no_bare_specifiers_remain_in_any_vendored_file():
    """Low-severity finding: the old marker list here was
    `('from"', "from '", 'import"', "import '")` -- it caught
    `from"pkg"` (no space) and `from 'pkg'` (single quote) but missed
    `from "pkg"` (a space *and* a double quote) and any `import(...)`
    call, so an upstream release built by a minifier that spells its
    bare import differently (a non-minified build, or just a different
    minifier's own spacing convention) could ship a bare specifier this
    test would wave through while the browser still fails to resolve it
    at runtime. `_IMPORT_RE` (this module's own copy of
    scripts/vendor_web.py's rewriter regex) covers all four quote/space
    combinations plus a dynamic `import("pkg")`.
    """
    lock = _require_lock()
    for name in _module_names(lock):
        source = (VENDOR_DIR / name).read_text(encoding="utf-8")
        for match in _IMPORT_RE.finditer(source):
            specifier = match.group(2)
            assert specifier.startswith("./") or specifier.startswith("../"), (
                f"{name}: bare specifier {specifier!r} was not rewritten"
            )


def test_vendor_lock_integrity_matches_the_pins_committed_in_vendor_web_py():
    """Medium-severity finding: VENDOR.lock is regenerated on every run
    of scripts/vendor_web.py from whatever the registry says at fetch
    time, so a lock that is merely internally self-consistent (matches
    its own files' sha256) proves nothing about *what* was fetched --
    that check has to be against a value committed independently of the
    fetch, i.e. `_PINNED_INTEGRITY`."""
    lock = _require_lock()
    for package, entry in lock["packages"].items():
        pinned_versions = {
            version for pkg, version, *_ in (*_PACKAGES, *_FONT_PACKAGES) if pkg == package
        }
        assert entry["version"] in pinned_versions, f"{package}: version not in _PACKAGES"
        pin = _PINNED_INTEGRITY[(package, entry["version"])]
        assert entry["integrity"] == pin, (
            f"{package}@{entry['version']}: VENDOR.lock integrity does not match the pin "
            "in scripts/vendor_web.py's _PINNED_INTEGRITY"
        )


def test_no_forbidden_dynamic_eval_in_any_vendored_file():
    lock = _require_lock()
    for name in _module_names(lock):
        source = (VENDOR_DIR / name).read_text(encoding="utf-8")
        violations = [snippet for snippet in _FORBIDDEN_SNIPPETS if snippet in source]
        assert not violations, f"{name} contains forbidden snippet(s): {violations}"


def test_no_source_map_comment_remains_in_any_vendored_file():
    lock = _require_lock()
    for name in _module_names(lock):
        source = (VENDOR_DIR / name).read_text(encoding="utf-8")
        assert "sourceMappingURL" not in source, f"{name} still carries a source map comment"


def test_every_locked_font_is_a_real_woff2_under_the_static_size_cap():
    """The fonts skip the text checks above, so check what they are
    instead: a WOFF2 signature, and small enough for routes.py's static
    manifest to serve (it silently drops anything larger)."""
    from app.web.routes import MAX_STATIC_FILE_BYTES

    lock = _require_lock()
    fonts = [name for name in lock["files"] if not name.endswith(".module.js")]
    assert fonts, "VENDOR.lock lists no fonts"
    for name in fonts:
        data = (VENDOR_DIR / name).read_bytes()
        assert data[:4] == b"wOF2", f"{name} is not a WOFF2 file"
        assert len(data) <= MAX_STATIC_FILE_BYTES, f"{name} is over MAX_STATIC_FILE_BYTES"
