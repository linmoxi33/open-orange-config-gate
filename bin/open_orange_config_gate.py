#!/usr/bin/env python3
"""open-orange-config-gate - static preflight for OpenOrange (rivet-dev/open-orange).

Reads real Wrangler configuration files (wrangler.jsonc / wrangler.json /
wrangler.toml), resolves the same top-level -> env inheritance Wrangler uses,
and reports which declared bindings cannot work under `orange dev`, with the
failure mode (throw vs silent data loss vs needs a provider service).

The status matrix is pinned to upstream commit e5e7954. Zero third-party
dependencies; no network access.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
from typing import Any

SCHEMA_VERSION = 1
UPSTREAM = "rivet-dev/open-orange"
PIN = "e5e795446d2b8d9e7be289d6fbc6f87630e1ef34"
DOCS = f"https://github.com/{UPSTREAM}/blob/{PIN}/docs/modules/bindings.mdx"
README = f"https://github.com/{UPSTREAM}/blob/{PIN}/README.md"

# status: drop_in | partial | needs_service | not_supported
# mode:   none | degraded | throws_until_configured | throws | silent_drop
BINDINGS: dict[str, tuple[str, str, str]] = {
    "kv_namespaces": ("drop_in", "none", 118),
    "d1_databases": ("drop_in", "none", 119),
    "r2_buckets": ("drop_in", "none", 121),
    "queues": ("drop_in", "none", 122),
    "ratelimits": ("drop_in", "none", 120),
    "secrets_store_secrets": ("drop_in", "none", 109),
    "version_metadata": ("drop_in", "none", 110),
    "assets": ("drop_in", "none", 112),
    "durable_objects": ("partial", "degraded", 113),
    "services": ("partial", "degraded", 115),
    "dispatch_namespaces": ("partial", "degraded", 117),
    "workflows": ("partial", "degraded", 123),
    "tail_consumers": ("partial", "degraded", 124),
    "worker_loaders": ("partial", "degraded", 125),
    "ai": ("needs_service", "throws_until_configured", 126),
    "send_email": ("needs_service", "throws_until_configured", 127),
    "browser": ("needs_service", "throws_until_configured", 128),
    "hyperdrive": ("needs_service", "throws_until_configured", 129),
    "analytics_engine_datasets": ("not_supported", "silent_drop", 130),
    "vectorize": ("not_supported", "throws", 131),
    "images": ("not_supported", "throws", 132),
    "media": ("not_supported", "throws", 132),
    "stream": ("not_supported", "throws", 132),
    "pipelines": ("not_supported", "throws", 133),
    "ai_search": ("not_supported", "throws", 134),
    "ai_search_namespaces": ("not_supported", "throws", 134),
    "mtls_certificates": ("not_supported", "throws", 135),
    "vpc_networks": ("not_supported", "throws", 136),
    "vpc_services": ("not_supported", "throws", 136),
    "text_blobs": ("not_supported", "throws", 137),
    "data_blobs": ("not_supported", "throws", 137),
    "wasm_modules": ("not_supported", "throws", 137),
}

# Config keys that stop `orange dev` before any binding is usable (docs L165-169).
FATAL_KEYS = {
    "cloudchamber": 166,
    "connect": 167,
    "define": 167,
    "exports": 167,
    "legacy_assets": 167,
    "pages_build_output_dir": 168,
    "site": 168,
    "streaming_tail_consumers": 168,
}
WARN_KEYS = {"containers": 173}

# Wrangler does not inherit these into env.<name>; everything else is inherited.
NON_INHERITED = {
    "name", "main", "compatibility_date", "compatibility_flags",
    "migrations", "assets", "site", "legacy_assets", "pages_build_output_dir",
}

SEVERITY = {"not_supported": "blocker", "needs_service": "warning",
            "partial": "warning", "drop_in": "info"}
FIX = {
    "not_supported": "move this binding behind a Cloudflare-only worker, or remove it before migrating",
    "needs_service": "configure the provider env var documented for this binding, or do not rely on it locally",
    "partial": "read the per-binding doc page and add a conformance test for the listed difference",
}


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- JSONC parsing
def strip_jsonc(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return _drop_trailing_commas("".join(out))


def _drop_trailing_commas(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "]}":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_config(path: str) -> tuple[dict[str, Any], str]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} is not UTF-8: {exc}") from exc
    try:
        if path.endswith(".toml"):
            data = tomllib.loads(text)
        else:
            data = json.loads(strip_jsonc(text))
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level value must be an object/table")
    return data, text


# ---------------------------------------------------------------- discovery
CONFIG_NAMES = ("wrangler.toml", "wrangler.jsonc", "wrangler.json")


def discover(paths: list[str]) -> list[str]:
    found: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            hit = [os.path.join(p, n) for n in CONFIG_NAMES if os.path.isfile(os.path.join(p, n))]
            if not hit:
                raise ConfigError(f"no wrangler.toml/json/jsonc in {p}")
            found.append(hit[0])
        elif os.path.isfile(p):
            found.append(p)
        else:
            raise ConfigError(f"no such file or directory: {p}")
    return found


def _line_of(text: str, key: str, toml: bool) -> int:
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=" if toml else rf'^\s*"{re.escape(key)}"\s*:')
    for i, line in enumerate(text.splitlines(), 1):
        if pat.search(line):
            return i
    return 0


# ---------------------------------------------------------------- bindings
def _binding_items(key: str, value: Any) -> list[str]:
    """Return binding names declared under one wrangler key."""
    names: list[str] = []
    if key == "queues" and isinstance(value, dict):
        for section in ("producers", "consumers"):
            for item in value.get(section) or []:
                if isinstance(item, dict) and item.get("binding"):
                    names.append(str(item["binding"]))
        return names
    if key == "durable_objects" and isinstance(value, dict):
        for item in value.get("bindings") or []:
            if isinstance(item, dict):
                name = item.get("name") or item.get("binding")
                if name:
                    names.append(str(name))
        return names
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                name = item.get("binding") or item.get("name")
                if name:
                    names.append(str(name))
    elif isinstance(value, dict):
        if key in ("text_blobs", "data_blobs", "wasm_modules"):
            return [str(k) for k in value]
        name = value.get("binding")
        if name:
            names.append(str(name))
        else:
            names.append("(unnamed)")
    return names


def declared_bindings(cfg: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key in BINDINGS:
        if key in cfg:
            names = _binding_items(key, cfg[key])
            if names:
                out[key] = names
    return out


def effective_envs(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    envs: dict[str, dict[str, Any]] = {"production": dict(cfg)}
    raw_envs = cfg.get("env")
    if isinstance(raw_envs, dict):
        for env_name, block in raw_envs.items():
            if not isinstance(block, dict):
                continue
            merged = {k: v for k, v in cfg.items() if k not in NON_INHERITED and k != "env"}
            merged.update(block)
            envs[str(env_name)] = merged
    return envs


def scan_source(src_dir: str) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Best-effort: env.<NAME> references and writeDataPoint() call sites."""
    refs: dict[str, dict[str, Any]] = {}
    wp: list[dict[str, Any]] = []
    exts = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in {"node_modules", ".git", "dist", "build"}]
        for fname in files:
            if not fname.endswith(exts):
                continue
            fp = os.path.join(root, fname)
            try:
                lines = open(fp, "r", encoding="utf-8", errors="replace").read().splitlines()
            except OSError:
                continue
            rel = os.path.relpath(fp, src_dir)
            for i, line in enumerate(lines, 1):
                for m in re.finditer(r"\benv\.([A-Za-z_][A-Za-z0-9_]*)", line):
                    refs.setdefault(m.group(1), {"file": rel, "line": i})
                if "writeDataPoint(" in line:
                    wp.append({"file": rel, "line": i})
    return refs, wp


# ---------------------------------------------------------------- analysis
def analyze(cfg: dict[str, Any], path: str, text: str, src_refs: dict[str, dict[str, Any]] | None,
            wp_calls: list[dict[str, Any]]) -> dict[str, Any]:
    toml = path.endswith(".toml")
    workers: list[dict[str, Any]] = []
    base = effective_envs(cfg)
    base_bindings = declared_bindings(base["production"])
    raw_envs = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    for env_name, env_cfg in base.items():
        findings: list[dict[str, Any]] = []
        binding_map = declared_bindings(env_cfg)
        for key, names in sorted(binding_map.items()):
            status, mode, line = BINDINGS[key]
            # Only report an env when it does not simply inherit the top-level block.
            inherited = env_name != "production" and base_bindings.get(key) == names
            if inherited:
                continue
            for name in names:
                ev = f"{DOCS}#L{line}"
                f: dict[str, Any] = {
                    "code": {"not_supported": "BINDING_NOT_SUPPORTED",
                             "needs_service": "BINDING_NEEDS_SERVICE",
                             "partial": "BINDING_PARTIAL",
                             "drop_in": "BINDING_DROP_IN"}[status],
                    "severity": SEVERITY[status],
                    "status": status,
                    "failure_mode": mode,
                    "environment": env_name,
                    "key": key,
                    "binding": name,
                    "line": _line_of(text, key, toml),
                    "evidence": ev,
                    "message": _message(key, name, status, mode),
                }
                if status == "not_supported":
                    f["fix"] = FIX["not_supported"]
                    if mode == "silent_drop":
                        f["code"] = "SILENT_DATA_LOSS_RISK"
                        f["fix"] = ("remove the Analytics Engine binding or replace writeDataPoint() "
                                    "with an explicit sink; OpenOrange drops the points without an error")
                elif status == "needs_service":
                    f["fix"] = FIX["needs_service"]
                else:
                    f["fix"] = FIX["partial"]
                if src_refs is not None and name in src_refs:
                    f["referenced_at"] = src_refs[name]
                findings.append(f)
        for key, line in FATAL_KEYS.items():
            if env_name != "production" and key not in raw_envs.get(env_name, {}):
                continue
            if key in env_cfg:
                findings.append({
                    "code": "CONFIG_STOPS_STARTUP", "severity": "blocker", "failure_mode": "throws",
                    "environment": env_name, "key": key, "binding": None,
                    "line": _line_of(text, key, toml), "evidence": f"{DOCS}#L{line}",
                    "message": f"`{key}` is a configuration key that stops `orange dev`; the Worker never starts",
                    "fix": "remove this key for the local OpenOrange run",
                })
        unsafe = env_cfg.get("unsafe")
        if unsafe is not None and (not isinstance(unsafe, dict) or any(k != "bindings" for k in unsafe)):
            if env_name == "production" or "unsafe" in raw_envs.get(env_name, {}):
                findings.append({
                    "code": "CONFIG_STOPS_STARTUP", "severity": "blocker", "failure_mode": "throws",
                    "environment": env_name, "key": "unsafe", "binding": None,
                    "line": _line_of(text, "unsafe", toml), "evidence": f"{DOCS}#L169",
                    "message": "`unsafe` settings other than `unsafe.bindings` stop `orange dev`",
                    "fix": "keep only `unsafe.bindings` for the local OpenOrange run",
                })
        trig = env_cfg.get("triggers")
        if isinstance(trig, dict) and "events" in trig:
            findings.append({
                "code": "CONFIG_STOPS_STARTUP", "severity": "blocker", "failure_mode": "throws",
                "environment": env_name, "key": "triggers.events", "binding": None,
                "line": _line_of(text, "events", toml), "evidence": f"{DOCS}#L169",
                "message": "`triggers.events` stops `orange dev`; only `triggers.crons` is supported",
                "fix": "remove triggers.events for the local run",
            })
        for key, line in WARN_KEYS.items():
            if env_name != "production" and key not in raw_envs.get(env_name, {}):
                continue
            if key in env_cfg:
                findings.append({
                    "code": "CONFIG_ACCEPTED_WITH_WARNING", "severity": "warning",
                    "failure_mode": "degraded", "environment": env_name, "key": key,
                    "binding": None, "line": _line_of(text, key, toml),
                    "evidence": f"{DOCS}#L{line}",
                    "message": f"`{key}` is accepted but only with a warning under `orange dev`",
                    "fix": "verify the container-dependent path manually",
                })
        if env_name == "production" and src_refs is not None:
            for name, site in src_refs.items():
                if name not in {n for ns in binding_map.values() for n in ns}:
                    findings.append({
                        "code": "UNDECLARED_BINDING_REF", "severity": "warning",
                        "failure_mode": "throws_until_configured", "environment": "production",
                        "key": None, "binding": name, "line": site["line"],
                        "evidence": f"{DOCS}#L152", "referenced_at": site,
                        "message": f"source reads `env.{name}` but no binding of that name is declared",
                        "fix": "declare the binding or fix the name; env.<X> is undefined at runtime",
                    })
        if env_name == "production" and wp_calls and "analytics_engine_datasets" not in binding_map:
            findings.append({
                "code": "WRITE_DATA_POINT_WITHOUT_BINDING", "severity": "warning",
                "failure_mode": "silent_drop", "environment": "production", "key": None,
                "binding": None, "line": 0, "evidence": f"{DOCS}#L130",
                "message": f"{len(wp_calls)} writeDataPoint() call(s) found but no analytics_engine_datasets binding",
                "call_sites": wp_calls,
                "fix": "either the call is dead code or the binding is missing from this config",
            })
        workers.append({
            "name": str(env_cfg.get("name") or (cfg.get("name") if env_name == "production"
                                                else f"{cfg.get('name')}-{env_name}") or "(unnamed)"),
            "config": path,
            "environment": env_name,
            "findings": findings,
        })
    summary = {"blockers": 0, "warnings": 0, "silent_data_loss": 0, "workers": len(workers)}
    for w in workers:
        for f in w["findings"]:
            if f["severity"] == "blocker":
                summary["blockers"] += 1
            elif f["severity"] == "warning":
                summary["warnings"] += 1
            if f["failure_mode"] == "silent_drop":
                summary["silent_data_loss"] += 1
    return {"schema_version": SCHEMA_VERSION, "tool": "open-orange-config-gate",
            "upstream": f"https://github.com/{UPSTREAM}", "pinned_commit": PIN,
            "workers": workers, "summary": summary}


def _message(key: str, name: str, status: str, mode: str) -> str:
    if status == "not_supported":
        if mode == "silent_drop":
            return (f"binding `{name}` ({key}) is not supported: writeDataPoint() drops each data point "
                    "without raising, so the Worker appears healthy while losing all writes")
        return f"binding `{name}` ({key}) is not supported: calls throw at runtime"
    if status == "needs_service":
        return (f"binding `{name}` ({key}) needs a configured provider; without it calls throw, "
                "with it they run against a non-Cloudflare service")
    return f"binding `{name}` ({key}) runs with known differences; check the binding doc before relying on it"


def run(paths: list[str], src: str | None, fail_on: str) -> tuple[int, dict[str, Any] | None]:
    try:
        files = discover(paths)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2, None
    src_refs = None
    wp_calls: list[dict[str, Any]] = []
    if src:
        if not os.path.isdir(src):
            print(f"error: --src is not a directory: {src}", file=sys.stderr)
            return 2, None
        refs, wp_calls = scan_source(src)
        src_refs = refs
    digest = hashlib.sha256()
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "tool": "open-orange-config-gate",
                           "upstream": f"https://github.com/{UPSTREAM}", "pinned_commit": PIN,
                           "workers": [], "summary": {}}
    totals = {"blockers": 0, "warnings": 0, "silent_data_loss": 0, "workers": 0}
    for path in files:
        try:
            cfg, text = load_config(path)
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2, None
        digest.update(path.encode())
        with open(path, "rb") as fh:
            digest.update(fh.read())
        res = analyze(cfg, path, text, src_refs, wp_calls)
        out["workers"].extend(res["workers"])
        for k in ("blockers", "warnings", "silent_data_loss", "workers"):
            totals[k] += res["summary"][k]
    out["summary"] = totals
    out["input_sha256"] = digest.hexdigest()
    blocking = totals["blockers"] > 0 or (fail_on == "any" and totals["warnings"] > 0)
    return (1 if blocking else 0), out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="open-orange-config-gate",
                                 description="Static preflight for OpenOrange (orange dev)")
    sub = ap.add_subparsers(dest="cmd")
    chk = sub.add_parser("check", help="check one or more wrangler configs")
    chk.add_argument("config", nargs="+", help="wrangler.jsonc/json/toml or a directory")
    chk.add_argument("--json", action="store_true", help="machine-readable output")
    chk.add_argument("--src", default=None, help="also cross-check env.<NAME> refs in this source tree")
    chk.add_argument("--fail-on", choices=("blocker", "any"), default="blocker")
    matrix = sub.add_parser("matrix", help="print the pinned support matrix as JSON")
    args = ap.parse_args(argv)
    if args.cmd == "matrix":
        doc = {"pinned_commit": PIN, "evidence": DOCS,
               "bindings": {k: {"status": s, "failure_mode": m,
                                "evidence": f"{DOCS}#L{l}"}
                            for k, (s, m, l) in sorted(BINDINGS.items())}}
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 0
    if args.cmd != "check":
        ap.print_usage(sys.stderr)
        return 2
    code, result = run(args.config, args.src, args.fail_on)
    if result is None:
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for w in result["workers"]:
            for f in w["findings"]:
                if f["severity"] == "info":
                    continue
                line = f" L{f['line']}" if f["line"] else ""
                bind = f" [{f['binding']}]" if f["binding"] else ""
                print(f"{f['severity'].upper():7} {w['name']}/{w['environment']}{line}{bind} "
                      f"{f['message']}")
        s = result["summary"]
        print(f"summary: {s['blockers']} blocker(s), {s['warnings']} warning(s), "
              f"{s['silent_data_loss']} silent-data-loss item(s), {s['workers']} worker env(s)")
    return code


if __name__ == "__main__":
    sys.exit(main())