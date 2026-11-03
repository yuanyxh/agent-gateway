"""Search runtime: hard cascade deadline and optional parallel fan-out."""

from __future__ import annotations

import time
import unittest
from unittest import mock

from agent_gateway.services.search import engine_config, engines


class RuntimeConfigTest(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.object(engine_config.config_loader, "value", return_value={}):
            runtime = engine_config.load_runtime()
        self.assertEqual(runtime.timeout_seconds, 25.0)
        self.assertFalse(runtime.parallel)
        self.assertEqual(runtime.concurrency, 4)

    def test_file_and_env_override(self):
        cfg = {"runtime": {"timeoutSeconds": 8, "parallel": True, "concurrency": 6}}
        env = {"GATEWAY_SEARCH_RUNTIME": '{"concurrency": 2}'}
        with (
            mock.patch.object(engine_config.config_loader, "value", return_value=cfg),
            mock.patch.dict(engine_config.os.environ, env),
        ):
            runtime = engine_config.load_runtime()
        self.assertEqual(runtime.timeout_seconds, 8.0)
        self.assertTrue(runtime.parallel)
        self.assertEqual(runtime.concurrency, 2)

    def test_invalid_values_fall_back(self):
        cfg = {"runtime": {"timeoutSeconds": "x", "concurrency": 99}}
        with mock.patch.object(engine_config.config_loader, "value", return_value=cfg):
            runtime = engine_config.load_runtime()
        self.assertEqual(runtime.timeout_seconds, 25.0)
        self.assertEqual(runtime.concurrency, 8)


class CascadeTest(unittest.TestCase):
    def test_serial_cascade_skips_engines_after_deadline(self):
        versions = []

        def run(engine, query, limit, deadline):
            versions.append(engine)
            time.sleep(0.02)
            return [], {"status": "ok", "parsed": 0}

        runtime = engine_config.SearchRuntime(
            timeout_seconds=0.01, parallel=False, concurrency=1
        )
        with (
            mock.patch.object(engines, "SEARCH_BUDGET", 0.01),
            mock.patch.object(engines, "_RUNTIME", runtime),
            mock.patch.object(engines, "_run_engine", side_effect=run),
            mock.patch.object(
                engines, "_resolve_jumps", side_effect=lambda r, d=None: r
            ),
        ):
            _, report = engines.search_report("q", engines=("bing", "sogou"))
        self.assertEqual(len(versions), 1)
        self.assertIn(
            "skipped_budget", {s["status"] for s in report["engines"].values()}
        )

    def test_parallel_collects_within_deadline(self):
        def run(engine, query, limit, deadline):
            time.sleep(0.01 if engine == "bing" else 0.3)
            item = {
                "title": "alpha",
                "url": f"https://{engine}.example/page",
                "snippet": "",
                "date": "",
                "engine": engine,
                "score": 1.0,
            }
            return [item], {"status": "ok", "parsed": 1}

        runtime = engine_config.SearchRuntime(
            timeout_seconds=0.06, parallel=True, concurrency=2
        )
        with (
            mock.patch.object(engines, "SEARCH_BUDGET", 0.06),
            mock.patch.object(engines, "_RUNTIME", runtime),
            mock.patch.object(engines, "_run_engine", side_effect=run),
            mock.patch.object(
                engines, "_score_results", side_effect=lambda items, q: items
            ),
            mock.patch.object(
                engines, "_resolve_jumps", side_effect=lambda r, d=None: r
            ),
        ):
            started = time.monotonic()
            results, report = engines.search_report("alpha", engines=("bing", "sogou"))
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.2)
        self.assertEqual([r["engine"] for r in results], ["bing"])
        self.assertEqual(report["engines"]["sogou"]["status"], "skipped_budget")

    def test_slow_engine_keeps_running_after_deadline(self):
        finished = []

        def run(engine, query, limit, deadline):
            if engine == "sogou":
                time.sleep(0.15)
                finished.append(engine)
            return [], {"status": "ok", "parsed": 0}

        runtime = engine_config.SearchRuntime(
            timeout_seconds=0.03, parallel=True, concurrency=2
        )
        with (
            mock.patch.object(engines, "SEARCH_BUDGET", 0.03),
            mock.patch.object(engines, "_RUNTIME", runtime),
            mock.patch.object(engines, "_run_engine", side_effect=run),
            mock.patch.object(
                engines, "_resolve_jumps", side_effect=lambda r, d=None: r
            ),
        ):
            started = time.monotonic()
            _, report = engines.search_report("q", engines=("bing", "sogou"))
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.1)
        self.assertEqual(report["engines"]["sogou"]["status"], "skipped_budget")
        # 超时只停止等待；已启动线程继续跑完，属预期的延迟回收。
        time.sleep(0.2)
        self.assertEqual(finished, ["sogou"])


class MarginaliaRetryTest(unittest.TestCase):
    PAGE = 'barraged by queries <a href="/search?q=x&sst=1">retry</a>'

    def test_retry_timeout_never_rounds_to_zero(self):
        deadline = time.monotonic() + 2.5
        with (
            mock.patch.object(engines, "_get", return_value=("", "retried", {})) as get,
            mock.patch.object(engines.time, "sleep"),
        ):
            out = engines._marginalia_retry(self.PAGE, deadline)
        self.assertEqual(out, "retried")
        self.assertEqual(get.call_args.kwargs["timeout"], 1)


class ProviderDeadlineTest(unittest.TestCase):
    def test_provider_runtime_is_clamped_to_remaining_budget(self):
        spec = engine_config.ProviderSpec(("helper",), timeout=25)
        with (
            mock.patch.dict(engines._PROVIDERS, {"ext": spec}),
            mock.patch.object(engines, "_run_provider", return_value=[]) as run,
        ):
            engines._run_engine("ext", "q", 5, time.monotonic() + 2)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 2.0)
