"""统一任务中心的大数据量服务端分页回归测试。

这些测试刻意把业务正文做大，并记录查询返回行数与 Python JSON 解析次数：
分页可以扫描轻量状态头和做全量聚合，但只能读取/反序列化当前页的业务详情。
"""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from app import db, departments, taskcenter


class TaskCenterScaleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "scale.db")
        db.conn()

    def tearDown(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_twenty_thousand_rows_count_exactly_but_load_only_page_details(self):
        connection = db.conn()
        body = "业务正文" * 160
        task_brief = json.dumps(
            {"direction": "规模化专家任务", "material": body},
            ensure_ascii=False,
        )
        job_brief = json.dumps(
            {"direction": "规模化内容工单", "material": body},
            ensure_ascii=False,
        )
        statuses = ("running", "awaiting_review", "done", "failed")

        connection.executemany(
            "INSERT INTO task("
            "tenant_id,emp_idx,brief_json,status,billing_status,retry_count,"
            "created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                (1, 0, task_brief, statuses[index % 4], "refunded", 0,
                 float(index + 1), float(index + 1))
                for index in range(12_000)
            ),
        )
        connection.executemany(
            "INSERT INTO job("
            "tenant_id,brief_json,status,billing_status,retry_count,current_idx,"
            "created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                (1, job_brief, statuses[index % 4], "refunded", 0, 0,
                 float(20_000 + index), float(20_000 + index))
                for index in range(8_000)
            ),
        )
        # 两类噪声都不能进入计数或页内详情：别的租户，以及当前账号无权看的部门。
        connection.executemany(
            "INSERT INTO task("
            "tenant_id,emp_idx,brief_json,status,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                (2, 0, task_brief, "running", float(40_000 + index),
                 float(40_000 + index))
                for index in range(1_000)
            ),
        )
        connection.executemany(
            "INSERT INTO task("
            "tenant_id,emp_idx,brief_json,status,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                (1, 9001, task_brief, "running", float(50_000 + index),
                 float(50_000 + index))
                for index in range(1_000)
            ),
        )
        connection.commit()

        industry_expert = {
            "idx": 9001,
            "dept_key": "restaurant",
            "name": "餐饮经营顾问",
        }
        real_get = departments.get

        def expert_get(index):
            return industry_expert if index == 9001 else real_get(index)

        query_stats = []
        json_loads = 0
        real_q = db.q
        real_jloads = db.jloads

        def traced_q(sql, args=()):
            rows = real_q(sql, args)
            query_stats.append({
                "sql": " ".join(sql.split()),
                "rows": len(rows),
                "body_rows": (
                    len(rows)
                    if rows and {
                        "brief_json", "params_json", "payload_json",
                    } & set(rows[0])
                    else 0
                ),
            })
            return rows

        def traced_jloads(value, default=None):
            nonlocal json_loads
            json_loads += 1
            return real_jloads(value, default)

        started = time.perf_counter()
        with (
            patch.object(departments, "get", side_effect=expert_get),
            patch.object(taskcenter.db, "q", side_effect=traced_q),
            patch.object(taskcenter.db, "jloads", side_effect=traced_jloads),
        ):
            result = taskcenter.list_items(
                1, {"content"}, limit=37, offset=123
            )
        elapsed = time.perf_counter() - started

        self.assertEqual(result["counts"], {
            "all": 20_000,
            "active": 5_000,
            "waiting": 5_000,
            "done": 5_000,
            "failed": 5_000,
            "cancelled": 0,
            "open": 10_000,
        })
        self.assertEqual(
            result["kind_counts"],
            {"expert": 12_000, "content": 8_000},
        )
        self.assertEqual(len(result["items"]), 37)
        self.assertEqual(result["offset"], 123)
        self.assertEqual(result["next_offset"], 160)
        self.assertTrue(result["has_more"])
        self.assertTrue(all(
            item["status_group"] == "waiting" for item in result["items"]
        ))

        # 允许全量扫描轻量 header；业务正文只能按页内 ID 读取和解析。
        self.assertLessEqual(
            sum(stat["body_rows"] for stat in query_stats),
            37,
            query_stats,
        )
        self.assertLessEqual(json_loads, 37)
        self.assertLessEqual(len(query_stats), 16, query_stats)
        self.assertLess(elapsed, 3.0, {
            "elapsed": elapsed,
            "queries": query_stats,
            "json_loads": json_loads,
        })

    def test_meeting_visibility_fails_closed_for_hidden_unknown_or_malformed_members(self):
        rows = (
            ("纯内容部会议", "[0]"),
            ("跨部门会议", "[0,9001]"),
            ("未知成员会议", "[0,999999]"),
            ("损坏成员会议", "{broken"),
        )
        ids = {
            question: db.insert("meeting", {
                "tenant_id": 1,
                "question": question,
                "emp_idxs_json": members,
                "status": "running",
                "phase": "brainstorm",
                "created_at": index + 1,
                "updated_at": index + 1,
            })
            for index, (question, members) in enumerate(rows)
        }
        industry_expert = {
            "idx": 9001,
            "dept_key": "restaurant",
            "name": "餐饮经营顾问",
        }
        real_get = departments.get

        def expert_get(index):
            return industry_expert if index == 9001 else real_get(index)

        with patch.object(departments, "get", side_effect=expert_get):
            content_rows = taskcenter.list_items(
                1, {"content"}, limit=20
            )["items"]
            mixed_rows = taskcenter.list_items(
                1, {"content", "restaurant"}, limit=20
            )["items"]

        content_meetings = {
            item["record_id"] for item in content_rows
            if item["kind"] == "meeting"
        }
        mixed_meetings = {
            item["record_id"] for item in mixed_rows
            if item["kind"] == "meeting"
        }
        self.assertEqual(content_meetings, {ids["纯内容部会议"]})
        self.assertEqual(
            mixed_meetings,
            {ids["纯内容部会议"], ids["跨部门会议"]},
        )
        self.assertNotIn(ids["未知成员会议"], mixed_meetings)
        self.assertNotIn(ids["损坏成员会议"], mixed_meetings)

    def test_all_eight_kinds_load_one_open_detail_not_eight_full_tables(self):
        connection = db.conn()
        body = "大体量正文" * 120

        def status(index):
            return "paused" if index == 999 else "done"

        connection.executemany(
            "INSERT INTO task("
            "tenant_id,emp_idx,brief_json,status,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                (
                    1, 0,
                    json.dumps({"direction": f"专家{index}", "material": body},
                               ensure_ascii=False),
                    status(index), index + 1, index + 1,
                )
                for index in range(1_000)
            ),
        )
        connection.executemany(
            "INSERT INTO job("
            "tenant_id,brief_json,status,current_idx,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                (
                    1,
                    json.dumps({"direction": f"内容{index}", "material": body},
                               ensure_ascii=False),
                    status(index), 0, 2_000 + index, 2_000 + index,
                )
                for index in range(1_000)
            ),
        )
        first_job_id = int(connection.execute(
            "SELECT MIN(id) FROM job WHERE tenant_id=1"
        ).fetchone()[0])
        connection.executemany(
            "INSERT INTO meeting("
            "tenant_id,question,emp_idxs_json,status,phase,"
            "execution_task_ids_json,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                (
                    1, f"会议{index}{body}", "[0]", status(index),
                    status(index), "[]", 4_000 + index, 4_000 + index,
                )
                for index in range(1_000)
            ),
        )
        connection.executemany(
            "INSERT INTO avatar_job("
            "tenant_id,params_json,status,created_at,updated_at"
            ") VALUES(?,?,?,?,?)",
            (
                (
                    1,
                    json.dumps({"script": f"数字人{index}{body}"},
                               ensure_ascii=False),
                    status(index), 6_000 + index, 6_000 + index,
                )
                for index in range(1_000)
            ),
        )
        connection.executemany(
            "INSERT INTO tv_job("
            "tenant_id,params_json,status,created_at,updated_at"
            ") VALUES(?,?,?,?,?)",
            (
                (
                    1,
                    json.dumps({"title": f"视频{index}", "script": body},
                               ensure_ascii=False),
                    status(index), 8_000 + index, 8_000 + index,
                )
                for index in range(1_000)
            ),
        )
        connection.executemany(
            "INSERT INTO tool_job("
            "tenant_id,kind,params_json,status,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                (
                    1, "hot",
                    json.dumps({"industry": f"工具{index}", "material": body},
                               ensure_ascii=False),
                    status(index), 10_000 + index, 10_000 + index,
                )
                for index in range(1_000)
            ),
        )
        connection.executemany(
            "INSERT INTO pub_task("
            "tenant_id,platform,payload_json,status,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                (
                    1, "小红书",
                    json.dumps({"title": f"发布{index}", "body": body},
                               ensure_ascii=False),
                    status(index), 12_000 + index, 12_000 + index,
                )
                for index in range(1_000)
            ),
        )
        connection.executemany(
            "INSERT INTO wechat_draft_delivery("
            "tenant_id,job_id,request_hash,request_key,title,status,"
            "billing_status,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                (
                    1, first_job_id, f"hash-{index}", f"key-{index}",
                    f"公众号{index}{body}", status(index), "succeeded",
                    14_000 + index, 14_000 + index,
                )
                for index in range(1_000)
            ),
        )
        connection.commit()

        query_stats = []
        json_loads = 0
        real_q = db.q
        real_jloads = db.jloads

        def traced_q(sql, args=()):
            rows = real_q(sql, args)
            body_keys = {
                "brief_json", "params_json", "payload_json",
                "question", "title", "log",
            }
            query_stats.append({
                "sql": " ".join(sql.split()),
                "rows": len(rows),
                "body_rows": (
                    len(rows)
                    if rows and body_keys & set(rows[0])
                    else 0
                ),
            })
            return rows

        def traced_jloads(value, default=None):
            nonlocal json_loads
            json_loads += 1
            return real_jloads(value, default)

        started = time.perf_counter()
        with (
            patch.object(taskcenter.db, "q", side_effect=traced_q),
            patch.object(taskcenter.db, "jloads", side_effect=traced_jloads),
        ):
            result = taskcenter.list_items(
                1, {"content", "avatar"}, limit=8, offset=0
            )
        elapsed = time.perf_counter() - started

        self.assertEqual(result["counts"], {
            "all": 8_000,
            "active": 0,
            "waiting": 8,
            "done": 7_992,
            "failed": 0,
            "cancelled": 0,
            "open": 8,
        })
        self.assertEqual(
            result["kind_counts"],
            {
                "expert": 1_000,
                "content": 1_000,
                "meeting": 1_000,
                "avatar": 1_000,
                "video": 1_000,
                "tool": 1_000,
                "publish": 1_000,
                "wechat": 1_000,
            },
        )
        self.assertEqual(
            {item["kind"] for item in result["items"]},
            {
                "expert", "content", "meeting", "avatar",
                "video", "tool", "publish", "wechat",
            },
        )
        self.assertLessEqual(
            sum(stat["body_rows"] for stat in query_stats), 8, query_stats
        )
        self.assertLessEqual(json_loads, 8)
        self.assertLessEqual(len(query_stats), 20, query_stats)
        self.assertLess(elapsed, 2.0, {
            "elapsed": elapsed,
            "queries": query_stats,
            "json_loads": json_loads,
        })
