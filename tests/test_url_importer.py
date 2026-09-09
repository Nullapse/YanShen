import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from gongkao.db import connect, init_db
from gongkao.services.url_importer import (
    FenbiCredentialStore,
    build_fenbi_bookmarklet,
    build_fenbi_solution_api_url,
    fetch_source_draft,
    normalize_fenbi_payload,
    parse_fenbi_credentials,
    parse_source_url,
)
from gongkao.web.application import create_server


SOURCE_URL = "https://spa.fenbi.com/ti/exam/solution/1_2_3r9r4m3?routecs=shenlun"


def _sample_payload():
    return {
        "meta": {
            "name": "2024浙江省考申论A卷",
            "staticUrl": {"urls": ["https://nodestatic.fbstatic.cn/sample.json"]},
        },
        "static_payloads": [
            {
                "materials": [
                    {"idx": 1, "title": "材料1", "content": "某市通过数字化平台整合服务窗口，提升群众办事效率。"},
                    {"idx": 2, "title": "材料2", "content": "某县坚持党建引领，发展特色产业。"},
                ],
                "solutions": [
                    {
                        "globalId": "q-1",
                        "question": {
                            "content": "根据给定资料1，概括该市提升服务效率的主要做法。",
                            "questionType": "归纳概括",
                        },
                        "requirements": "全面、准确，不超过200字。",
                        "score": 20,
                        "answer": "一是整合服务窗口；二是建设数字化平台。",
                        "scoringPoints": ["整合服务窗口", "建设数字化平台"],
                    }
                ],
            }
        ],
    }


def _real_shape_payload():
    return {
        "meta": {
            "name": "2025浙江省考申论A卷",
            "staticUrl": {"type": 1, "urls": ["https://nodestatic.fbstatic.cn/real-shape.json"]},
        },
        "static_payloads": [{
            "materials": [
                {"globalId": "m-a", "content": "第一则材料：基层服务窗口通过数字化改造提升办事效率。"},
                {"globalId": "m-b", "content": "第二则材料：社区通过议事协商解决停车治理问题。"},
            ],
            "solutions": [{
                "globalId": "q-real",
                "content": "根据给定资料，概括社区治理的主要做法。",
                "materialKeys": ["m-b"],
                "requirements": "全面、准确，不超过150字。",
                "correctAnswer": {"answer": "不应优先采用的答案"},
                "solution": "这是解析字段，不应覆盖参考答案字段。",
                "solutionAccessories": [{
                    "label": "reference",
                    "content": "一是开展议事协商；二是推动居民参与。",
                }],
            }],
        }],
    }


class UrlImporterTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.sqlite3")
        init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_parse_fenbi_url_and_build_bookmarklet(self):
        info = parse_source_url(SOURCE_URL)
        self.assertEqual(info.provider, "粉笔")
        self.assertEqual(info.source_kind, "fenbi_solution")
        self.assertEqual(info.key, "1_2_3r9r4m3")
        self.assertEqual(info.routecs, "shenlun")

        bookmarklet = build_fenbi_bookmarklet("session-123", "http://127.0.0.1:5000/papers/import-url/bridge/payload")
        self.assertTrue(bookmarklet.startswith("javascript:"))
        self.assertIn("session-123", bookmarklet)
        self.assertIn("getSolution", bookmarklet)
        self.assertIn("credentials:'include'", bookmarklet)
        self.assertIn("fenbiHosts", bookmarklet)
        self.assertIn("不要直接在研申本地页面点击", bookmarklet)

    def test_normalize_fenbi_payload_preserves_candidate_answer_warning(self):
        draft = normalize_fenbi_payload(
            _sample_payload()["meta"],
            _sample_payload()["static_payloads"],
            SOURCE_URL,
        )
        self.assertEqual(draft["paper_name"], "2024浙江省考申论A卷")
        self.assertEqual(draft["region"], "浙江")
        self.assertEqual(len(draft["materials"]), 2)
        self.assertEqual(len(draft["questions"]), 1)
        question = draft["questions"][0]
        self.assertIn("提升服务效率", question["prompt"])
        self.assertIn("数字化平台", question["reference_answer"]["answer_text"])
        self.assertEqual(question["reference_answer"]["organization"], "粉笔")
        self.assertEqual(question["reference_answer"]["is_reviewed"], 0)
        self.assertIn("未核验", question["reference_answer"]["notes"])

    def test_normalize_real_fenbi_shape_maps_material_keys_and_reference_accessory(self):
        payload = _real_shape_payload()
        draft = normalize_fenbi_payload(payload["meta"], payload["static_payloads"], SOURCE_URL)
        self.assertEqual(len(draft["materials"]), 2)
        self.assertEqual(len(draft["questions"]), 1)
        question = draft["questions"][0]
        self.assertEqual(question["material_numbers"], [2])
        self.assertIn("第二则材料", question["materials"])
        self.assertNotIn("第一则材料", question["materials"])
        self.assertEqual(
            question["reference_answer"]["answer_text"],
            "一是开展议事协商；二是推动居民参与。",
        )
        self.assertNotIn("不应优先", question["reference_answer"]["answer_text"])

    def test_normalize_rejects_questions_without_material_body(self):
        payload = {
            "name": "2025申论卷",
            "solutions": [{"globalId": "q", "content": "概括主要做法。"}],
        }
        with self.assertRaisesRegex(ValueError, "没有识别到材料正文"):
            normalize_fenbi_payload(payload, [], SOURCE_URL)

    def test_parse_and_store_fenbi_credentials_filters_domains_and_expiry(self):
        exported = json.dumps([
            {"domain": ".fenbi.com", "name": "sess", "value": "session-value", "secure": True},
            {"domain": ".fenbi.com", "name": "device_id", "value": "device-value"},
            {"domain": "evil.example", "name": "token", "value": "do-not-send"},
            {"domain": ".fenbi.com", "name": "expired", "value": "old", "expirationDate": time.time() - 10},
        ])
        credentials = parse_fenbi_credentials(exported)
        self.assertEqual(credentials["device_id"], "device-value")
        self.assertEqual({item["name"] for item in credentials["cookies"]}, {"sess", "device_id"})

        path = os.path.join(self.tmpdir, "fenbi-session.json")
        store = FenbiCredentialStore(path)
        store.save(credentials)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        loaded = store.load()
        self.assertEqual(loaded["device_id"], "device-value")
        self.assertEqual({item["name"] for item in loaded["cookies"]}, {"sess", "device_id"})
        store.clear()
        self.assertFalse(os.path.exists(path))

    def test_direct_fetch_uses_device_id_cookie_and_static_payload(self):
        payload = _real_shape_payload()
        credentials = parse_fenbi_credentials(
            json.dumps([{"domain": ".fenbi.com", "name": "sess", "value": "session-value"}]),
            "device-value",
        )
        responses = [
            (json.dumps(payload["meta"]).encode("utf-8"), "application/json"),
            (json.dumps(payload["static_payloads"][0]).encode("utf-8"), "application/json"),
        ]
        with patch("gongkao.services.url_importer._read_url", side_effect=responses) as read_url:
            result = fetch_source_draft(SOURCE_URL, credentials=credentials)
        self.assertTrue(result["ok"])
        self.assertEqual(result["draft"]["questions"][0]["material_numbers"], [2])
        self.assertIn("deviceId=device-value", read_url.call_args_list[0].args[0])
        self.assertEqual(read_url.call_args_list[0].kwargs["credentials"], credentials)
        self.assertIn("routecs=shenlun&type=1", read_url.call_args_list[1].args[0])
        self.assertEqual(len(read_url.call_args_list), 2)

    def test_api_url_can_include_device_id(self):
        info = parse_source_url(SOURCE_URL)
        url = build_fenbi_solution_api_url(info, "device-value")
        self.assertIn("deviceId=device-value", url)

    def test_direct_fetch_reports_browser_bridge_for_logged_in_endpoint(self):
        error = HTTPError("https://tiku.fenbi.com", 401, "login required", {}, None)
        with patch("gongkao.services.url_importer.urlopen", side_effect=error):
            result = fetch_source_draft(SOURCE_URL)
        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_browser_bridge"])
        self.assertIn("登录态", result["message"])

    def test_http_bridge_preview_and_persist_imported_source(self):
        server = create_server(port=0, db_path=self.db_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            bridge_result = {
                "ok": False,
                "draft": None,
                "source": parse_source_url(SOURCE_URL).as_dict(),
                "requires_browser_bridge": True,
                "message": "需要浏览器登录态",
            }
            with patch("gongkao.web.controllers.library.fetch_source_draft", return_value=bridge_result):
                request = Request(
                    f"{base}/papers/import-url",
                    data=json.dumps({"source_url": SOURCE_URL}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=10) as response:
                    inspected = json.loads(response.read().decode("utf-8"))
            self.assertTrue(inspected["session_id"])
            self.assertTrue(inspected["bookmarklet"].startswith("javascript:"))

            payload = _sample_payload()
            request = Request(
                f"{base}/papers/import-url/bridge/payload",
                headers={"Origin": "https://spa.fenbi.com", "Access-Control-Request-Method": "POST"},
                method="OPTIONS",
            )
            with urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, 204)
                self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "https://spa.fenbi.com")
                self.assertEqual(response.headers.get("Access-Control-Allow-Private-Network"), "true")

            request = Request(
                f"{base}/papers/import-url/bridge/payload",
                data=json.dumps({"session_id": inspected["session_id"], "source_url": SOURCE_URL, "payload": payload}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Origin": "https://spa.fenbi.com"},
                method="POST",
            )
            with urlopen(request, timeout=10) as response:
                bridged = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "https://spa.fenbi.com")
            self.assertTrue(bridged["ok"])
            self.assertIn("/papers/new?import_session=", bridged["redirect"])
            self.assertEqual(len(bridged["draft"]["questions"]), 1)

            with urlopen(bridged["redirect"], timeout=10) as response:
                page = response.read().decode("utf-8")
            self.assertIn("从 URL 自动录入", page)
            self.assertIn('id="url-import-seed"', page)
            self.assertIn('"source_kind": "fenbi_solution"', page)
            self.assertIn('src="/static/app.js?', page)
            self.assertIn(f'value="{SOURCE_URL}"', page)

            draft = bridged["draft"]
            request = Request(
                f"{base}/papers/new",
                data=json.dumps({
                    "paper_name": draft["paper_name"],
                    "year": draft["year"],
                    "region": draft["region"],
                    "exam_type": draft["exam_type"],
                    "paper_category": draft["paper_category"],
                    "source_url": draft["source_url"],
                    "source_kind": draft["source_kind"],
                    "source_note": draft["import_note"],
                    "materials": draft["materials"],
                    "questions": draft["questions"],
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=10) as response:
                created = json.loads(response.read().decode("utf-8"))
            self.assertTrue(created["ok"])

            with connect(self.db_path) as conn:
                question = conn.execute("SELECT * FROM questions WHERE id = (SELECT MAX(id) FROM questions)").fetchone()
                self.assertEqual(question["source_url"], SOURCE_URL)
                self.assertEqual(question["source_kind"], "fenbi_solution")
                reference = conn.execute("SELECT * FROM reference_answers WHERE question_id = ?", (question["id"],)).fetchone()
                self.assertEqual(reference["is_reviewed"], 0)
                source = conn.execute("SELECT * FROM question_sources WHERE question_id = ?", (question["id"],)).fetchone()
                self.assertEqual(source["provider"], "粉笔")
                self.assertEqual(source["source_url"], SOURCE_URL)
        finally:
            server.shutdown()
            server.server_close()

    def test_http_url_import_accepts_and_remembers_explicit_credentials(self):
        server = create_server(port=0, db_path=self.db_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        store = FenbiCredentialStore(os.path.join(self.tmpdir, "fenbi-session.json"))
        draft = normalize_fenbi_payload(
            _sample_payload()["meta"],
            _sample_payload()["static_payloads"],
            SOURCE_URL,
        )
        fetch_result = {"ok": True, "draft": draft, "source": parse_source_url(SOURCE_URL).as_dict()}
        exported = json.dumps([
            {"domain": ".fenbi.com", "name": "sess", "value": "session-value"},
        ])
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with patch("gongkao.web.controllers.library.FENBI_CREDENTIALS", store), patch(
                "gongkao.web.controllers.library.fetch_source_draft", return_value=fetch_result
            ) as fetch:
                request = Request(
                    f"{base}/papers/import-url",
                    data=json.dumps({
                        "source_url": SOURCE_URL,
                        "cookie_text": exported,
                        "device_id": "device-value",
                        "remember_session": True,
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=10) as response:
                    inspected_text = response.read().decode("utf-8")
                    inspected = json.loads(inspected_text)
            self.assertTrue(inspected["ok"])
            self.assertTrue(inspected["stored_credentials"])
            self.assertEqual(inspected["credentials"]["cookie_count"], 1)
            self.assertNotIn("session-value", inspected_text)
            self.assertEqual(fetch.call_args.kwargs["credentials"]["device_id"], "device-value")
            self.assertEqual(store.load()["device_id"], "device-value")

            with patch("gongkao.web.controllers.library.FENBI_CREDENTIALS", store):
                request = Request(
                    f"{base}/papers/import-url",
                    data=json.dumps({"clear_credentials": True}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=10) as response:
                    cleared = json.loads(response.read().decode("utf-8"))
            self.assertTrue(cleared["ok"])
            self.assertFalse(os.path.exists(store.path))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
