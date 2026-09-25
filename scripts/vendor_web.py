"""Vendor the frontend's five ES modules from the real npm registry
(W1 plan step 1), so `app/web/static/vendor/*.module.js` ships pinned,
integrity-checked, build-tool-free source instead of a `node_modules`
this repo has no build step to produce it from.

    uv run python scripts/vendor_web.py

Stdlib-only (`urllib`, `tarfile`, `hashlib`, `json`) -- no new Python
dependency, and it uses the ambient proxy environment variables
(HTTPS_PROXY/no_proxy), which `urllib.request` already respects on its
own, exactly like scripts/web_passphrase.py needs nothing beyond the
standard library for its own single job.

**What it fetches and why these exact files.** Preact/htm/Signals ship
plain ES module builds under npm only as an implementation detail of a
bundler-oriented package layout; there is no CDN in this CSP
(`script-src 'self'`, no `connect-src` beyond it) and no build step in
this repo, so the only supply chain this script trusts is "the npm
registry's own published tarball, checked against the registry's own
advertised integrity hash" -- never a CDN's copy, which is a different
trust root than the one `npm install` itself would check.

**Verification, two independent layers.** First, each tarball's SHA-512
is checked against the registry API's `dist.integrity` for that exact
version *before* anything is extracted from it -- a `.tgz` that does
not match is a corrupted download or a compromised mirror, and this
script refuses to unpack it either way. Second, after extraction and
rewriting, `VENDOR.lock` records each shipped file's own SHA-256, so a
later `git diff` on a vendored file is instantly visible in the lock
file's diff too, and tests/test_web_vendor.py (the plan's other half
of this step) can assert the two never drift apart.

**Rewriting bare specifiers.** Every one of these five files is an ES
module; three of them (`hooks.module.js`, `signals.module.js`) import
from Preact and Signals-core by their *package* name (`"preact"`,
`"preact/hooks"`, `"@preact/signals-core"`), which only resolves under
Node's/a bundler's module resolution -- never in a browser loading
`<script type="module">` with no import map (this CSP has none, and a
`(importmap)"` script tag is itself something `script-src 'self'` with
Trusted Types would have to specially exempt). Each bare specifier is
rewritten to the plain relative path its sibling file actually lands
at, and `_assert_no_bare_specifiers` fails the whole run if anything
that still looks like `from"some-package"` (or the space/`import`
variants) survives the rewrite -- the fixed table `_SPECIFIER_MAP`
below is the *complete* list this script promises to handle, so an
upstream release that adds a new bare import must fail loudly here
rather than ship a module the browser cannot load.

**Fonts.** `_FONT_PACKAGES` adds the self-hosted `.woff2` files
(`@fontsource/*`) under `vendor/fonts/`. They go through the exact same
pinned-integrity tarball checks, and are then copied byte for byte:
the rewriting and source-map steps below are for the JS modules only.

**Source maps.** Every file also strips a trailing `//# sourceMappingURL=
...` comment: three of the five files carry it appended to the very
last line of minified code (not on its own line), so the strip looks
for the marker's *text*, not a whole-line match -- a real `.map` file
is never fetched or shipped, and a stray comment pointing at one that
does not exist here would be dead weight at best and a way to leak an
internal path at worst.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import pathlib
import re
import tarfile
import urllib.parse
import urllib.request

REGISTRY = "https://registry.npmjs.org"

# (package, version, path inside the tarball, destination filename in
# app/web/static/vendor/) -- exactly the W1 plan's step-1 table, in the
# same order it lists them.
_PACKAGES = (
    ("preact", "10.29.8", "package/dist/preact.module.js", "preact.module.js"),
    ("preact", "10.29.8", "package/hooks/dist/hooks.module.js", "hooks.module.js"),
    ("htm", "3.1.1", "package/dist/htm.module.js", "htm.module.js"),
    (
        "@preact/signals-core",
        "1.14.4",
        "package/dist/signals-core.module.js",
        "signals-core.module.js",
    ),
    (
        "@preact/signals",
        "2.11.2",
        "package/dist/signals.module.js",
        "signals.module.js",
    ),
)

# (package, version) -> the `sha512-<base64>` SRI string this script
# trusts -- committed here, in this file, rather than read back from
# VENDOR.lock or from the registry's own live metadata (medium-severity
# finding: the old check compared the tarball's hash against
# `dist.integrity` from that *same* metadata response, so an
# intercepting proxy, a compromised registry or a compromised mirror
# only had to serve a tarball and an `integrity` field that agreed with
# each other -- never with anything this repo actually committed -- to
# pass verification and have its modified module.js shipped, with
# VENDOR.lock regenerated to match). Recorded once, the first time this
# script ran against the real registry for each of these exact
# versions; bumping a version means updating its pin here by hand (from
# `npm view <pkg>@<version> dist.integrity`, or by reading it off
# https://registry.npmjs.org, checked against a second source), never
# by letting a run of this script write its own new pin for itself.
_PINNED_INTEGRITY: dict[tuple[str, str], str] = {
    ("preact", "10.29.8"): (
        "sha512-ej2aVZ+vZ8WO7tvlQWRM9N63A0KzF9q4mWJfDUHgYaIofWY9hu74QdnQrjoPMmZi2/nZ5gN0bJCQF49xQqx09Q=="
    ),
    ("htm", "3.1.1"): (
        "sha512-983Vyg8NwUE7JkZ6NmOqpCZ+sh1bKv2iYTlUkzlWmA5JD2acKoxd4KVxbMmxX/85mtfdnDmTFoNKcg5DGAvxNQ=="
    ),
    ("@preact/signals-core", "1.14.4"): (
        "sha512-HNB6HYeYKhQbJ1aKl+YRjrS4+QWHLKX6qKoUsfS/m0vqzsVaEBiZiaKbG/e+NKk2ch5ALQr/ihWaMHxiCuuWHA=="
    ),
    ("@preact/signals", "2.11.2"): (
        "sha512-rVTRTt/T0HIRgbugwS5FigbfF/kfdEFYFtiqxa+lbpqTajepqnR0firuAS2iNdHyejWB7Yc9p9QePkVNBtTAwg=="
    ),
    # The three font packages below: read off registry.npmjs.org and
    # checked against registry.npmmirror.com's metadata for the same
    # versions (they agreed) before being committed here.
    ("@fontsource/plus-jakarta-sans", "5.3.0"): (
        "sha512-9WRw3G74Ve4cyPApAZSXLPY5DiLOZXDn7Q7dIBjFBpA0Pj56ljDItEdy2UUajjJxRuTg2HkMLO5C3K2nAQORWg=="
    ),
    ("@fontsource/manrope", "5.3.0"): (
        "sha512-obJ1Dv3+uCA6HlHgW8u4BGYxJR9In2HW7gjJhlflEvkrj1X1iSEwu0fToL+JYGC/FEKFfIz1sBuPduvcL2gIAA=="
    ),
    ("@fontsource/geist-mono", "5.3.0"): (
        "sha512-UtJ1BBBCVpMYdIcW7nEB45UAoAw5M53ZXs2t0ciPW+IokuAAIc56M8+kW5tXbRJCTpDw4XTtU7proT6NdQAHTg=="
    ),
}


def _font_files(package: str, version: str, subsets: tuple[str, ...], weights: tuple[int, ...]):
    family = package.rsplit("/", 1)[1]
    return tuple(
        (
            package,
            version,
            f"package/files/{family}-{subset}-{weight}-normal.woff2",
            f"fonts/{family}-{subset}-{weight}-normal.woff2",
        )
        for subset in subsets
        for weight in weights
    )


# Self-hosted web fonts (the web redesign's R0), same shape as
# _PACKAGES. Unlike the JS modules these are copied byte for byte into
# app/web/static/vendor/fonts/: no decoding, no specifier rewriting,
# no source-map strip -- all of that is JS-only. Only `.woff2`, and
# only the subsets and weights app.css's @font-face rules name:
# Jakarta has no Cyrillic (the browser falls back to Manrope per
# glyph), and Geist Mono ships latin only for the same reason.
_FONT_PACKAGES = (
    *_font_files("@fontsource/plus-jakarta-sans", "5.3.0", ("latin",), (400, 500, 600)),
    *_font_files("@fontsource/manrope", "5.3.0", ("latin", "cyrillic"), (400, 500, 600)),
    *_font_files("@fontsource/geist-mono", "5.3.0", ("latin",), (400, 500)),
)

FONTS_SUBDIR = "fonts"

# The complete set of bare specifiers any of the five files is allowed
# to import -- see the module docstring's "Rewriting bare specifiers".
# Each maps to the plain relative path its sibling module lands at in
# app/web/static/vendor/, once this script has run.
_SPECIFIER_MAP = {
    "preact": "./preact.module.js",
    "preact/hooks": "./hooks.module.js",
    "@preact/signals-core": "./signals-core.module.js",
}

VENDOR_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "vendor"

_SOURCE_MAP_MARKER = "//# sourceMappingURL="

# Matches `from"pkg"`, `from "pkg"`, `import"pkg"` and `import "pkg"` --
# the four surface forms an ES module's bare specifier can take, per the
# task's own check-both-forms instruction. Capturing the keyword and the
# whitespace separately (rather than the whole prefix as one blob) is
# what lets the replacement keep whatever spacing the original file used
# instead of collapsing `from "x"` to `from"x"`.
_IMPORT_RE = re.compile(r'\b(from|import)(\s*)(["\'])([^"\']+)\3')


class VendorError(RuntimeError):
    """A verification or rewrite step failed; nothing is written."""


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "anchor-vendor-script"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https registry URL
        return response.read()


def _package_metadata(name: str, version: str) -> dict:
    url = f"{REGISTRY}/{urllib.parse.quote(name, safe='@/')}/{version}"
    return json.loads(_fetch(url))


def _integrity_to_sha512(integrity: str) -> bytes:
    """`dist.integrity` is `sha512-<base64>` (a Subresource Integrity
    string); every npm registry response for a modern package carries
    it, so this script never falls back to the older `dist.shasum`
    (a SHA-1, deliberately weaker) some very old packages ship instead.
    """
    algorithm, _, encoded = integrity.partition("-")
    if algorithm != "sha512":
        raise VendorError(f"unexpected integrity algorithm {algorithm!r} (want sha512)")
    return base64.b64decode(encoded)


def _verify_tarball(data: bytes, integrity: str, *, package: str) -> None:
    """Check `data` against `integrity` (a `sha512-<base64>` SRI
    string) -- the *caller* decides what `integrity` is trusted to be.
    Called twice per package, deliberately, from two different trust
    roots (see `_pinned_integrity` below): this function itself does
    not care which.
    """
    expected = _integrity_to_sha512(integrity)
    actual = hashlib.sha512(data).digest()
    if actual != expected:
        raise VendorError(
            f"{package}: tarball sha512 does not match the expected dist.integrity "
            f"(refusing to extract a tarball that fails this check)"
        )


def _pinned_integrity(package: str, version: str) -> str:
    key = (package, version)
    pinned = _PINNED_INTEGRITY.get(key)
    if pinned is None:
        raise VendorError(
            f"{package}@{version}: no pinned integrity in _PINNED_INTEGRITY -- add one "
            "(from `npm view <pkg>@<version> dist.integrity`, checked against a second "
            "source) before vendoring a new version"
        )
    return pinned


def _assert_registry_tarball_url(url: str, *, package: str) -> None:
    """The tarball URL itself, not only its bytes, must come from the
    one registry this script trusts -- `dist.tarball` is metadata the
    registry (or whatever sits between this script and it) supplies,
    same as `dist.integrity` was before it got pinned above, and
    nothing stops it from pointing anywhere. A `tarball` on some other
    host, even one that happens to match the pinned SRI hash by
    coincidence or by the registry itself misbehaving, must never be
    fetched: `_fetch` would send it this process's proxy-configured
    request headers, and only registry.npmjs.org over https is the
    trust root the rest of this script's verification is built on.
    """
    parsed = urllib.parse.urlparse(url)
    expected_host = urllib.parse.urlparse(REGISTRY).netloc
    if parsed.scheme != "https" or parsed.netloc != expected_host:
        raise VendorError(
            f"{package}: tarball URL {url!r} is not https://{expected_host}/... "
            "(refusing to fetch a tarball from an unexpected host)"
        )


def _extract_member_bytes(data: bytes, member_path: str, *, package: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        try:
            member = tar.getmember(member_path)
        except KeyError as exc:
            raise VendorError(f"{package}: {member_path!r} not found in the tarball") from exc
        extracted = tar.extractfile(member)
        if extracted is None:
            raise VendorError(f"{package}: {member_path!r} is not a regular file")
        return extracted.read()


def _extract_member(data: bytes, member_path: str, *, package: str) -> str:
    return _extract_member_bytes(data, member_path, package=package).decode("utf-8")


def _strip_source_map_comment(source: str) -> str:
    index = source.find(_SOURCE_MAP_MARKER)
    if index != -1:
        source = source[:index]
    return source.rstrip("\n") + "\n"


def _rewrite_bare_specifiers(source: str, *, dest_name: str) -> str:
    def _replace(match: re.Match) -> str:
        keyword, space, quote, specifier = match.groups()
        replacement = _SPECIFIER_MAP.get(specifier)
        if replacement is None:
            # Not one of ours to rewrite (a relative import, or an
            # unrecognized bare one) -- leave it untouched here;
            # _assert_no_bare_specifiers below is what actually fails
            # the run if this turns out to have been a real problem.
            return match.group(0)
        return f"{keyword}{space}{quote}{replacement}{quote}"

    return _IMPORT_RE.sub(_replace, source)


def _assert_no_bare_specifiers(source: str, *, dest_name: str) -> None:
    """After rewriting, every remaining `from`/`import` specifier must
    be relative (`./` or `../`) -- never a bare package name, which is
    exactly what a browser's module resolver cannot load with no
    import map (see the module docstring).
    """
    for match in _IMPORT_RE.finditer(source):
        specifier = match.group(4)
        if not (specifier.startswith("./") or specifier.startswith("../")):
            raise VendorError(
                f"{dest_name}: bare specifier {specifier!r} survived rewriting "
                f"(add it to _SPECIFIER_MAP)"
            )


def _verified_tarball(
    package: str,
    version: str,
    tarball_cache: dict[tuple[str, str], bytes],
    lock_packages: dict[str, dict],
) -> bytes:
    """Fetch and verify each distinct (package, version) tarball once,
    even though preact contributes two destination files from the one
    tarball and each font package contributes several."""
    cache_key = (package, version)
    if cache_key not in tarball_cache:
        pinned_integrity = _pinned_integrity(package, version)

        metadata = _package_metadata(package, version)
        dist = metadata["dist"]
        _assert_registry_tarball_url(dist["tarball"], package=package)

        # Two independent checks against two different trust roots,
        # not one (medium-severity finding): the registry's *own*
        # advertised `dist.integrity` must itself equal what this
        # script has pinned -- a registry, mirror or intercepting
        # proxy that serves a modified tarball would have to also
        # forge this match, which is no longer enough on its own,
        # because the downloaded *bytes* are then checked against
        # the very same pin again below, never against whatever
        # `dist.integrity` said.
        if dist["integrity"] != pinned_integrity:
            raise VendorError(
                f"{package}@{version}: registry dist.integrity {dist['integrity']!r} "
                f"does not match the pinned {pinned_integrity!r} in _PINNED_INTEGRITY "
                "(refusing to trust a metadata response that disagrees with the pin)"
            )

        tarball_bytes = _fetch(dist["tarball"])
        _verify_tarball(tarball_bytes, pinned_integrity, package=package)
        tarball_cache[cache_key] = tarball_bytes
        lock_packages[package] = {"version": version, "integrity": pinned_integrity}
    return tarball_cache[cache_key]


def main() -> None:
    """Fetch, verify and rewrite every package entirely into memory
    first; only once *every* one of them has succeeded does this touch
    `VENDOR_DIR` at all, and then it does so as a single unlink-and-
    write pass with nothing left half done in between.

    This is what makes VendorError's own docstring ("nothing is
    written") actually true (low-severity finding: the old `main()`
    `unlink()`-ed every existing vendor file up front, then wrote each
    new one as it went, so a package that failed partway through -- a
    bad integrity check, a network error -- left `vendor/` with some
    files replaced, others simply gone, and no VENDOR.lock at all,
    which made `tests/test_web_vendor.py`'s lock-based checks *skip*
    instead of fail on exactly the broken state that most needed to
    fail loudly).
    """
    staged: dict[str, bytes] = {}
    lock_packages: dict[str, dict] = {}
    lock_files: dict[str, dict] = {}

    # Fetch each distinct package's tarball once (_verified_tarball).
    tarball_cache: dict[tuple[str, str], bytes] = {}

    for package, version, member_path, dest_name in _PACKAGES:
        tarball = _verified_tarball(package, version, tarball_cache, lock_packages)

        source = _extract_member(tarball, member_path, package=package)
        source = _rewrite_bare_specifiers(source, dest_name=dest_name)
        _assert_no_bare_specifiers(source, dest_name=dest_name)
        source = _strip_source_map_comment(source)

        dest_bytes = source.encode("utf-8")
        staged[dest_name] = dest_bytes
        lock_files[dest_name] = {
            "package": package,
            "sha256": hashlib.sha256(dest_bytes).hexdigest(),
        }

    for package, version, member_path, dest_name in _FONT_PACKAGES:
        tarball = _verified_tarball(package, version, tarball_cache, lock_packages)
        dest_bytes = _extract_member_bytes(tarball, member_path, package=package)
        staged[dest_name] = dest_bytes
        lock_files[dest_name] = {
            "package": package,
            "sha256": hashlib.sha256(dest_bytes).hexdigest(),
        }

    lock = {"packages": lock_packages, "files": lock_files}
    lock_bytes = (json.dumps(lock, indent=2, sort_keys=True) + "\n").encode("utf-8")

    # Everything above this line only ever reads network responses and
    # builds in-memory dicts -- nothing under VENDOR_DIR has been
    # touched yet. From here on, every write is to bytes already fully
    # verified, so this pass cannot itself fail partway through in a
    # way that leaves a mismatched file behind.
    (VENDOR_DIR / FONTS_SUBDIR).mkdir(parents=True, exist_ok=True)
    for old in (*VENDOR_DIR.glob("*"), *(VENDOR_DIR / FONTS_SUBDIR).glob("*")):
        if old.is_file():
            old.unlink()
    for dest_name, dest_bytes in staged.items():
        dest_path = VENDOR_DIR / dest_name
        dest_path.write_bytes(dest_bytes)
        print(f"wrote {dest_path} ({len(dest_bytes)} bytes)")
    lock_path = VENDOR_DIR / "VENDOR.lock"
    lock_path.write_bytes(lock_bytes)
    print(f"wrote {lock_path}")


if __name__ == "__main__":
    main()
