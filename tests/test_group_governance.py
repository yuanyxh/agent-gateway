"""Tests for the offline episode-group governance prototype."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.memory import graph, group_governance, store  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def add(self, node_id, episode, title, **meta):
        store.upsert_node(
            node_id,
            layer="hot",
            status="active",
            title=title,
            metadata={"episode_id": episode, **meta},
            db_path=self.db,
        )


class LoadGroupsTest(_Base):
    def test_groups_singles_and_skips(self):
        self.add("a1", "ep_a", "检索层重构 实现")
        self.add("a2", "ep_a", "检索层重构 测试")
        self.add("c1", "ep_c", "独立事实")
        self.add("s1", "ep_a", "系统摘要", generated_by="consolidation")
        self.add("p1", "ep_a", "受保护事实", protected=True)

        loaded = group_governance.load_groups(self.db)

        self.assertEqual([g["candidate_id"] for g in loaded["groups"]], ["ep_a"])
        self.assertEqual(
            [m["id"] for m in loaded["groups"][0]["members"]], ["a1", "a2"]
        )
        self.assertEqual([m["id"] for m in loaded["singles"]], ["c1"])
        self.assertEqual(loaded["explicit_suspect"], [])
        self.assertEqual(loaded["oversized"], [])

    def test_explicit_suspect_and_oversized_are_separate(self):
        self.add("g1", "gateway-decouple", "解耦配置")
        self.add("g2", "gateway-decouple", "解耦路径")
        for index in range(9):
            self.add(f"x{index}", "ep_big", f"大组事实 {index}")

        loaded = group_governance.load_groups(self.db)

        self.assertEqual(loaded["groups"], [])
        self.assertEqual(loaded["explicit_suspect"], ["gateway-decouple"])
        self.assertEqual(loaded["oversized"], ["ep_big"])

    def test_group_with_missing_episode_is_ignored(self):
        self.add("n1", "", "无组事实")
        self.add("n2", "", "另一条无组事实")

        loaded = group_governance.load_groups(self.db)

        self.assertEqual(loaded["groups"], [])
        self.assertEqual(loaded["singles"], [])

    def test_body_prefers_the_full_fact_over_the_title(self):
        self.add("f1", "ep_f", "短标题", fact="完整正文" * 1001)
        self.add("f2", "ep_f", "队友")

        members = group_governance.load_groups(self.db)["groups"][0]["members"]
        member = next(item for item in members if item["id"] == "f1")

        self.assertEqual(member["text_source"], "fact")
        self.assertEqual(member["text"], "完整正文" * 1001)
        self.assertNotIn("truncated", member)

    def test_body_marks_title_as_degraded_when_fact_is_missing(self):
        self.add("t1", "ep_t", "只有标题")
        self.add("t2", "ep_t", "队友")

        members = group_governance.load_groups(self.db)["groups"][0]["members"]
        member = next(item for item in members if item["id"] == "t1")

        self.assertEqual(member["text_source"], "title")
        self.assertEqual(member["text"], "只有标题")


class SingleGroupTest(_Base):
    def test_single_explicit_group_is_not_paired(self):
        self.add("e1", "gateway-decouple", "解耦配置")

        loaded = group_governance.load_groups(self.db)

        self.assertEqual(loaded["singles"], [])
        self.assertEqual(loaded["explicit_suspect"], ["gateway-decouple"])

    def test_single_member_keeps_text_markers(self):
        self.add("s1", "ep_s", "只有标题")

        member = group_governance.load_groups(self.db)["singles"][0]

        self.assertEqual(member["text_source"], "title")

    def test_load_groups_pages_until_short_page(self):
        pages = [
            [{"node_id": "a1", "title": "一", "metadata": {"episode_id": "ep_a"}}],
            [{"node_id": "a2", "title": "二", "metadata": {"episode_id": "ep_a"}}],
            [],
        ]
        offsets = []

        def fake_list(**kwargs):
            offsets.append(kwargs["offset"])
            return pages.pop(0)

        with (
            mock.patch.object(group_governance, "PAGE_SIZE", 1),
            mock.patch.object(
                group_governance.store, "list_nodes", side_effect=fake_list
            ),
        ):
            loaded = group_governance.load_groups("db")

        self.assertEqual(offsets, [0, 1, 2])
        self.assertEqual(len(loaded["groups"][0]["members"]), 2)


class CandidatesTest(_Base):
    def setUp(self):
        super().setUp()
        self.add("a1", "ep_a", "检索层重构 实现 pipeline")
        self.add("a2", "ep_a", "检索层重构 测试 pipeline")
        self.add("c1", "ep_c", "检索层重构 开关实测 term_split")
        self.add("d1", "ep_d", "检索层重构 开关实测 relative gate")
        self.add("z1", "ep_z", "文章生成管线的模板改造")

    def test_single_pairs_prefer_similar_titles(self):
        with mock.patch.object(group_governance, "_try_hot", return_value=None):
            candidates = group_governance.build_candidates(db_path=self.db)

        pairs = {
            frozenset((item["members"][0]["id"], item["members"][1]["id"]))
            for item in candidates["pairs"]
        }
        self.assertIn(frozenset(("c1", "d1")), pairs)
        self.assertNotIn(frozenset(("c1", "z1")), pairs)

    def test_pair_budget_is_capped(self):
        with mock.patch.object(group_governance, "_try_hot", return_value=None):
            candidates = group_governance.build_candidates(
                db_path=self.db, single_pair_limit=1
            )

        self.assertEqual(len(candidates["pairs"]), 1)
        self.assertTrue(candidates["truncated"])

    def test_readonly_never_creates_or_writes(self):
        missing = os.path.join(self._tmp.name, "missing.sqlite")
        with self.assertRaises(FileNotFoundError):
            group_governance.build_candidates(db_path=missing, readonly=True)

        def snapshot():
            return {
                name: os.stat(os.path.join(self._tmp.name, name)).st_mtime_ns
                for name in os.listdir(self._tmp.name)
            }

        with mock.patch.object(group_governance, "_try_hot", return_value=None):
            before = snapshot()
            group_governance.build_candidates(db_path=self.db, readonly=True)
            self.assertEqual(snapshot(), before)


class DecideTest(unittest.TestCase):
    def candidates(self):
        return {
            "groups": [
                {
                    "candidate_id": "ep_a",
                    "kind": "group",
                    "members": [
                        {"id": "a1", "text": "实现"},
                        {"id": "a2", "text": "测试"},
                    ],
                }
            ],
            "pairs": [],
        }

    def test_split_is_accepted(self):
        async def fake_llm(*args, **kwargs):
            return (
                '{"decisions":[{"candidate_id":"ep_a","action":"split",'
                '"groups":[["a1"],["a2"]],"reason":"不同工作"}]}'
            )

        result = asyncio.run(group_governance.decide(self.candidates(), llm=fake_llm))

        self.assertEqual(result["parse_failed"], 0)
        self.assertEqual(result["decisions"][0]["action"], "split")
        self.assertEqual(result["decisions"][0]["groups"], [["a1"], ["a2"]])

    def test_split_must_cover_every_member(self):
        async def fake_llm(*args, **kwargs):
            return (
                '{"decisions":[{"candidate_id":"ep_a","action":"split",'
                '"groups":[["a1"]],"reason":"漏成员"}]}'
            )

        result = asyncio.run(group_governance.decide(self.candidates(), llm=fake_llm))

        self.assertEqual(result["decisions"], [])
        self.assertEqual(result["rejected"], {"bad_split": 1})

    def test_unknown_member_id_is_rejected(self):
        async def fake_llm(*args, **kwargs):
            return (
                '{"decisions":[{"candidate_id":"ep_a","action":"split",'
                '"groups":[["a1"],["x9"]],"reason":"未知 id"}]}'
            )

        result = asyncio.run(group_governance.decide(self.candidates(), llm=fake_llm))

        self.assertEqual(result["decisions"], [])
        self.assertEqual(result["rejected"], {"unknown_member": 1})

    def test_malformed_split_does_not_crash(self):
        async def fake_llm(*args, **kwargs):
            return (
                '{"decisions":[{"candidate_id":"ep_a","action":"split",'
                '"groups":[[["a1"]],[["a2"]]],"reason":"嵌套"}]}'
            )

        result = asyncio.run(group_governance.decide(self.candidates(), llm=fake_llm))

        self.assertEqual(result["decisions"], [])
        self.assertEqual(result["rejected"], {"unknown_member": 1})

    def test_llm_exception_is_counted_per_batch(self):
        async def fake_llm(*args, **kwargs):
            raise RuntimeError("timeout")

        result = asyncio.run(
            group_governance.decide(self.candidates(), llm=fake_llm, batch_size=1)
        )

        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["parse_failed"], 1)

    def test_pair_merge_ignores_the_merge_target(self):
        async def fake_llm(*args, **kwargs):
            return (
                '{"decisions":[{"candidate_id":"pair:a|b","action":"merge",'
                '"merge_with":"ep_a","reason":"同一主题"}]}'
            )

        candidates = {
            "groups": [],
            "pairs": [
                {
                    "candidate_id": "pair:a|b",
                    "kind": "pair",
                    "members": [
                        {"id": "a", "text": "一"},
                        {"id": "b", "text": "二"},
                    ],
                }
            ],
        }
        result = asyncio.run(group_governance.decide(candidates, llm=fake_llm))

        self.assertEqual(result["decisions"][0]["merge_with"], "")

    def test_batches_respect_char_budget(self):
        items = [
            {
                "candidate_id": f"c{index}",
                "kind": "group",
                "members": [{"id": "m", "text": "x" * 100}],
            }
            for index in range(5)
        ]

        batches = group_governance._batches(items, 10, 250)

        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])

    def test_non_json_counts_as_failure(self):
        async def fake_llm(*args, **kwargs):
            return "not json"

        result = asyncio.run(group_governance.decide(self.candidates(), llm=fake_llm))

        self.assertEqual(result["parse_failed"], 1)

    def test_missing_llm_marks_unavailable(self):
        result = asyncio.run(group_governance.decide(self.candidates(), llm=None))

        self.assertEqual(result["status"], "llm_unavailable")
        self.assertEqual(result["decisions"], [])

    def test_call_budget_stops_early(self):
        groups = []
        for index in range(4):
            groups.append(
                {
                    "candidate_id": f"ep_{index}",
                    "kind": "group",
                    "members": [
                        {"id": f"{index}a", "text": "一"},
                        {"id": f"{index}b", "text": "二"},
                    ],
                }
            )

        async def fake_llm(*args, **kwargs):
            return '{"decisions":[]}'

        result = asyncio.run(
            group_governance.decide(
                {"groups": groups, "pairs": []}, llm=fake_llm, batch_size=1, max_calls=2
            )
        )

        self.assertEqual(result["calls"], 2)


class GovernDailyTest(_Base):
    def _member(self, node_id, episode, text):
        return {
            "id": node_id,
            "episode_id": episode,
            "text": text,
            "text_source": "fact",
            "truncated": False,
            "created_at": "2026-11-09",
        }

    def _run(self, candidates, fake_llm, *, sync_ok=True):
        with (
            mock.patch.object(
                group_governance.config, "memory_governance_apply", return_value=True
            ),
            mock.patch.object(
                group_governance.llm_client, "is_configured", return_value=True
            ),
            mock.patch.object(group_governance.llm_client, "achat", fake_llm),
            mock.patch.object(
                group_governance, "build_candidates", return_value=candidates
            ),
            mock.patch.object(group_governance, "_sync_mem0", return_value=sync_ok),
        ):
            return asyncio.run(group_governance.govern_daily(db_path=self.db, since=""))

    def test_split_moves_the_smaller_group_and_keeps_previous(self):
        self.add("a1", "ep_a", "实现", fact="实现细节")
        self.add("a2", "ep_a", "测试", fact="测试结论")
        candidates = {
            "groups": [
                {
                    "candidate_id": "ep_a",
                    "kind": "group",
                    "members": [
                        self._member("a1", "ep_a", "实现细节"),
                        self._member("a2", "ep_a", "测试结论"),
                    ],
                }
            ],
            "pairs": [],
            "singles": [],
        }

        async def fake_llm(*args, **kwargs):
            return (
                '{"decisions":[{"candidate_id":"ep_a","action":"split",'
                '"groups":[["a1"],["a2"]],"reason":"不同工作"}]}'
            )

        result = self._run(candidates, fake_llm)

        first = store.get_node("a1", db_path=self.db)
        second = store.get_node("a2", db_path=self.db)
        self.assertEqual(first["metadata"]["episode_id"], "ep_a")
        self.assertNotEqual(second["metadata"]["episode_id"], "ep_a")
        self.assertEqual(second["metadata"]["episode_prev"], "ep_a")
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["written"], 1)
        self.assertIn("ep_a", result["touched"])
        self.assertEqual(len(result["touched"]), 2)
        self.assertFalse(
            any(
                edge["type"] == "episode"
                for edge in graph.neighbors("a2", db_path=self.db)
            )
        )

    def test_local_write_failure_restores_mem0(self):
        self.add("a1", "ep_a", "实现", fact="实现细节")
        self.add("a2", "ep_a", "测试", fact="测试结论")
        candidates = {
            "groups": [
                {
                    "candidate_id": "ep_a",
                    "kind": "group",
                    "members": [
                        self._member("a1", "ep_a", "实现细节"),
                        self._member("a2", "ep_a", "测试结论"),
                    ],
                }
            ],
            "pairs": [],
            "singles": [],
        }

        async def fake_llm(*args, **kwargs):
            return (
                '{"decisions":[{"candidate_id":"ep_a","action":"split",'
                '"groups":[["a1"],["a2"]],"reason":"不同工作"}]}'
            )

        calls = []

        def sync(member_id, patch):
            calls.append((member_id, dict(patch)))
            return True

        with (
            mock.patch.object(
                group_governance.config, "memory_governance_apply", return_value=True
            ),
            mock.patch.object(
                group_governance.llm_client, "is_configured", return_value=True
            ),
            mock.patch.object(group_governance.llm_client, "achat", fake_llm),
            mock.patch.object(
                group_governance, "build_candidates", return_value=candidates
            ),
            mock.patch.object(group_governance, "_sync_mem0", side_effect=sync),
            mock.patch.object(
                group_governance.store,
                "batch_update_metadata",
                side_effect=RuntimeError("db down"),
            ),
        ):
            result = asyncio.run(
                group_governance.govern_daily(db_path=self.db, since="")
            )

        self.assertEqual(result["written"], 0)
        self.assertGreater(result["errors"], 0)
        restored = [patch for _, patch in calls if patch.get("episode_id") == "ep_a"]
        self.assertEqual(len(restored), 1)

    def test_mem0_failure_skips_the_graph_write(self):
        self.add("a1", "ep_a", "实现", fact="实现细节")
        self.add("a2", "ep_a", "测试", fact="测试结论")
        candidates = {
            "groups": [
                {
                    "candidate_id": "ep_a",
                    "kind": "group",
                    "members": [
                        self._member("a1", "ep_a", "实现细节"),
                        self._member("a2", "ep_a", "测试结论"),
                    ],
                }
            ],
            "pairs": [],
            "singles": [],
        }

        async def fake_llm(*args, **kwargs):
            return (
                '{"decisions":[{"candidate_id":"ep_a","action":"split",'
                '"groups":[["a1"],["a2"]],"reason":"不同工作"}]}'
            )

        result = self._run(candidates, fake_llm, sync_ok=False)

        self.assertEqual(result["written"], 0)
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(
            store.get_node("a2", db_path=self.db)["metadata"]["episode_id"], "ep_a"
        )

    def test_pair_merge_moves_the_second_single(self):
        self.add("p1", "ep_p1", "一", fact="事实一")
        self.add("p2", "ep_p2", "二", fact="事实二")
        candidates = {
            "groups": [],
            "pairs": [
                {
                    "candidate_id": "pair:p1|p2",
                    "kind": "pair",
                    "members": [
                        self._member("p1", "ep_p1", "事实一"),
                        self._member("p2", "ep_p2", "事实二"),
                    ],
                }
            ],
            "singles": [
                self._member("p1", "ep_p1", "事实一"),
                self._member("p2", "ep_p2", "事实二"),
            ],
        }

        async def fake_llm(*args, **kwargs):
            return (
                '{"decisions":[{"candidate_id":"pair:p1|p2","action":"merge",'
                '"reason":"同一事件"}]}'
            )

        result = self._run(candidates, fake_llm)

        merged = store.get_node("p2", db_path=self.db)
        self.assertEqual(merged["metadata"]["episode_id"], "ep_p1")
        self.assertEqual(merged["metadata"]["episode_prev"], "ep_p2")
        self.assertEqual(result["applied"], 1)
        self.assertTrue(
            any(
                edge["type"] == "episode"
                and {edge["from_id"], edge["to_id"]} == {"p1", "p2"}
                for edge in graph.neighbors("p2", db_path=self.db)
            )
        )

    def test_disabled_switch_skips_the_pass(self):
        with mock.patch.object(
            group_governance.config, "memory_governance_apply", return_value=False
        ):
            result = asyncio.run(
                group_governance.govern_daily(db_path=self.db, since="")
            )

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["applied"], 0)


class ReportTest(unittest.TestCase):
    def test_report_has_counts_and_review_table(self):
        result = {
            "decisions": [
                {
                    "candidate_id": "ep_a",
                    "action": "split",
                    "groups": [["a1"], ["a2"]],
                    "reason": "不同工作",
                }
            ],
            "calls": 1,
            "parse_failed": 0,
            "rejected": {},
        }

        report = group_governance.render_report(
            {
                "groups": [
                    {
                        "candidate_id": "ep_a",
                        "kind": "group",
                        "members": [
                            {"id": "a1", "text": "实现细节"},
                            {"id": "a2", "text": "测试结论"},
                        ],
                    }
                ],
                "pairs": [],
                "truncated": False,
            },
            result,
        )

        self.assertIn("已生成建议：1", report)
        self.assertIn("实现细节", report)
        self.assertIn("组 1 = #1", report)
        self.assertIn("组 2 = #2", report)
