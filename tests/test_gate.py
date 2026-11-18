import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "open_orange_config_gate.py")
sys.path.insert(0, os.path.join(ROOT, "bin"))
import open_orange_config_gate as gate  # noqa: E402


def run_cli(*args):
    return subprocess.run([sys.executable, BIN, *args], cwd=ROOT,
                          capture_output=True, text=True)


class JsoncTests(unittest.TestCase):
    def test_strips_line_comments(self):
        self.assertEqual(json.loads(gate.strip_jsonc('{"a": 1} // x')), {"a": 1})

    def test_strips_block_comments(self):
        self.assertEqual(json.loads(gate.strip_jsonc('{/* c */ "a": 2}')), {"a": 2})

    def test_keeps_slashes_inside_strings(self):
        doc = gate.strip_jsonc('{"url": "https://example.com/x"}')
        self.assertEqual(json.loads(doc)["url"], "https://example.com/x")

    def test_drops_trailing_commas(self):
        self.assertEqual(json.loads(gate.strip_jsonc('{"a": [1, 2,],}')), {"a": [1, 2]})

    def test_trailing_comma_before_comment(self):
        text = '{"a": [1, 2, /* last */ ]}'
        self.assertEqual(json.loads(gate.strip_jsonc(text)), {"a": [1, 2]})


class ConfigTests(unittest.TestCase):
    def test_discover_directory_picks_jsonc(self):
        files = gate.discover([os.path.join(ROOT, "examples", "in", "clean")])
        self.assertTrue(files[0].endswith("wrangler.jsonc"))

    def test_load_toml_bindings(self):
        cfg, _ = gate.load_config(os.path.join(ROOT, "examples", "in", "toml", "wrangler.toml"))
        self.assertEqual(gate.declared_bindings(cfg)["d1_databases"], ["DB"])

    def test_env_inherits_bindings(self):
        cfg = {"name": "w", "kv_namespaces": [{"binding": "CACHE", "id": "c"}],
               "env": {"staging": {"name": "w-staging"}}}
        envs = gate.effective_envs(cfg)
        self.assertEqual(gate.declared_bindings(envs["staging"])["kv_namespaces"], ["CACHE"])

    def test_env_does_not_inherit_name_or_main(self):
        cfg = {"name": "w", "main": "a.ts", "env": {"staging": {}}}
        envs = gate.effective_envs(cfg)
        self.assertNotIn("name", envs["staging"])
        self.assertNotIn("main", envs["staging"])
        res = gate.analyze(cfg, "wrangler.jsonc", "", None, [])
        self.assertEqual([w["name"] for w in res["workers"]], ["w", "w-staging"])

    def test_env_override_replaces_array_not_merges(self):
        cfg = {"name": "w",
               "kv_namespaces": [{"binding": "A", "id": "a"}],
               "env": {"staging": {"kv_namespaces": [{"binding": "B", "id": "b"}]}}}
        envs = gate.effective_envs(cfg)
        self.assertEqual(gate.declared_bindings(envs["staging"])["kv_namespaces"], ["B"])

    def test_queues_bindings_collected_from_both_sections(self):
        cfg = {"queues": {"producers": [{"binding": "OUT", "queue": "q"}],
                          "consumers": [{"queue": "q", "max_batch_size": 10}]}}
        self.assertEqual(gate.declared_bindings(cfg)["queues"], ["OUT"])

    def test_durable_object_uses_name_field(self):
        cfg = {"durable_objects": {"bindings": [{"name": "ROOM", "class_name": "Room"}]}}
        self.assertEqual(gate.declared_bindings(cfg)["durable_objects"], ["ROOM"])


class AnalyzeTests(unittest.TestCase):
    def _analyze(self, cfg, path="wrangler.jsonc", src=None, wp=None):
        return gate.analyze(cfg, path, "", src, wp or [])

    def test_clean_config_has_no_blockers(self):
        cfg, text = gate.load_config(os.path.join(ROOT, "examples", "in", "clean", "wrangler.jsonc"))
        res = gate.analyze(cfg, "wrangler.jsonc", text, None, [])
        self.assertEqual(res["summary"]["blockers"], 0)

    def test_analytics_is_silent_drop_blocker(self):
        res = self._analyze({"name": "w", "analytics_engine_datasets": [{"binding": "A"}]})
        f = res["workers"][0]["findings"][0]
        self.assertEqual((f["code"], f["failure_mode"], f["severity"]),
                         ("SILENT_DATA_LOSS_RISK", "silent_drop", "blocker"))

    def test_vectorize_throws(self):
        res = self._analyze({"name": "w", "vectorize": [{"binding": "V"}]})
        f = res["workers"][0]["findings"][0]
        self.assertEqual((f["failure_mode"], f["severity"]), ("throws", "blocker"))

    def test_fatal_config_key_blocks_startup(self):
        res = self._analyze({"name": "w", "exports": {"a": "./a.js"}})
        codes = [f["code"] for f in res["workers"][0]["findings"]]
        self.assertIn("CONFIG_STOPS_STARTUP", codes)

    def test_triggers_events_blocks_startup(self):
        res = self._analyze({"name": "w", "triggers": {"events": ["x"]}})
        self.assertTrue(any(f["key"] == "triggers.events" for f in res["workers"][0]["findings"]))

    def test_containers_only_warns(self):
        res = self._analyze({"name": "w", "containers": [{"class_name": "C"}]})
        self.assertEqual(res["summary"]["blockers"], 0)
        self.assertEqual(res["summary"]["warnings"], 1)

    def test_unsafe_bindings_is_allowed(self):
        res = self._analyze({"name": "w", "unsafe": {"bindings": [{"name": "X", "type": "x"}]}})
        self.assertEqual(res["summary"]["blockers"], 0)

    def test_unsafe_non_binding_stops_startup(self):
        res = self._analyze({"name": "w", "unsafe": {"eval": True}})
        self.assertTrue(any(f["key"] == "unsafe" for f in res["workers"][0]["findings"]))

    def test_undeclared_source_ref_is_warning(self):
        res = self._analyze({"name": "w"}, src={"MISSING": {"file": "src/a.ts", "line": 3}})
        self.assertTrue(any(f["code"] == "UNDECLARED_BINDING_REF" for f in res["workers"][0]["findings"]))

    def test_declared_source_ref_attached_to_binding(self):
        res = self._analyze({"name": "w", "vectorize": [{"binding": "V"}]},
                            src={"V": {"file": "src/a.ts", "line": 7}})
        f = [x for x in res["workers"][0]["findings"] if x["binding"] == "V"][0]
        self.assertEqual(f["referenced_at"]["line"], 7)


class CliTests(unittest.TestCase):
    def test_cli_clean_exit_zero_json(self):
        p = run_cli("check", "examples/in/clean", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        doc = json.loads(p.stdout)
        self.assertEqual(doc["summary"]["blockers"], 0)

    def test_cli_risky_exit_one_json(self):
        p = run_cli("check", "examples/in/risky", "--src", "examples/in/risky/src", "--json")
        self.assertEqual(p.returncode, 1, p.stderr)
        doc = json.loads(p.stdout)
        self.assertEqual(doc["summary"]["blockers"], 2)
        self.assertEqual(doc["summary"]["silent_data_loss"], 1)

    def test_cli_missing_path_exit_two(self):
        p = run_cli("check", "/nonexistent/wrangler.jsonc")
        self.assertEqual(p.returncode, 2)
        self.assertIn("error:", p.stderr)

    def test_cli_bad_json_exit_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "wrangler.jsonc")
            open(bad, "w").write('{"name": "w",,}')
            p = run_cli("check", bad)
        self.assertEqual(p.returncode, 2)

    def test_cli_fail_on_any_turns_warning_into_exit_one(self):
        p = run_cli("check", "examples/in/risky", "--fail-on", "any")
        self.assertEqual(p.returncode, 1)

    def test_cli_matrix_json(self):
        p = run_cli("matrix")
        self.assertEqual(p.returncode, 0)
        doc = json.loads(p.stdout)
        self.assertEqual(doc["bindings"]["analytics_engine_datasets"]["failure_mode"], "silent_drop")

    def test_sha256_is_stable_across_runs(self):
        a = json.loads(run_cli("check", "examples/in/risky", "--json").stdout)
        b = json.loads(run_cli("check", "examples/in/risky", "--json").stdout)
        self.assertEqual(a["input_sha256"], b["input_sha256"])


if __name__ == "__main__":
    unittest.main()