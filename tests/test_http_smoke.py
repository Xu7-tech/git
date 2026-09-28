"""端到端冒烟测试：真实启动 uvicorn，用标准库 HTTP 客户端走一遍演示主线。

这一条直接对应验收口径"现场演示主线零中断"。
"""
from __future__ import annotations

import json
import pathlib
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HttpSmokeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.main:app",
             "--host", "127.0.0.1", "--port", str(cls.port), "--log-level", "warning"],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"{cls.base}/system/status", timeout=2).read()
                return
            except (urllib.error.URLError, ConnectionError, OSError):
                if cls.proc.poll() is not None:
                    raise AssertionError("uvicorn 启动失败：\n"
                                         + cls.proc.stdout.read().decode("utf-8", "replace"))
                time.sleep(0.4)
        raise AssertionError("uvicorn 在 40 秒内没有就绪")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    # ---- 工具 ----

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, path, body=None):
        req = urllib.request.Request(
            self.base + path, method="POST",
            data=json.dumps(body or {}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---- 用例 ----

    def test_01_static_assets_are_served(self):
        for path, marker in (("/", b"<!DOCTYPE html>"), ("/app.js", b"function"),
                             ("/styles.css", b":root")):
            with self.subTest(path=path):
                with urllib.request.urlopen(self.base + path, timeout=20) as r:
                    self.assertEqual(r.status, 200)
                    self.assertIn(marker, r.read())

    def test_02_status_reports_degraded_mode(self):
        self.post("/system/reset")
        status = self.get("/system/status")
        self.assertFalse(status["llm_live"])
        self.assertTrue(status["degraded_mode"])
        self.assertEqual(status["auth_threshold"], 2000.0)
        # 降级指的是"意图理解走本地规则解析器"，与语音通道无关 ——
        # 曾经把它写成 LLM 与 ASR 都可用才为 false，配上大模型后仍报 true，会误导评审
        self.assertEqual(status["llm_provider"], "rule-engine-fallback")
        self.assertIn("规则解析器", status["ai_stack"]["意图理解"])
        self.assertEqual(status["ai_stack"]["任务规划"], "Plan-and-Execute")
        # Web 界面实际走浏览器识别，与 /asr 接口可用的通道分开报告
        self.assertEqual(status["asr_in_use"], "browser-native-stt")

    def test_03_voice_endpoints_fall_back(self):
        asr = self.post("/asr", {"dialect": "cantonese"})
        self.assertEqual(asr["provider"], "offline-fallback")
        asr2 = self.post("/asr", {"dialect": "mandarin", "transcript_hint": "给老李转五十块"})
        self.assertEqual(asr2["provider"], "browser-native-stt")
        tts = self.post("/tts", {"text": "您好", "dialect": "cantonese"})
        self.assertEqual(tts["provider"], "browser-speech-synthesis")

    def test_04_demo_mainline(self):
        self.post("/system/reset")

        # 1 粤语查账
        bill = self.post("/agent/plan", {"text": "睇下我今个月用咗几多钱", "dialect": "auto"})
        self.assertEqual(bill["intent"]["name"], "bill_analysis")
        self.assertIn("可疑交易", bill["elder_text"])

        # 2 小额免密转账
        small = self.post("/agent/plan", {"text": "畀老李转五十蚊"})
        self.assertEqual(small["risk"]["level"], "L0")
        done = self.post("/agent/confirm",
                         {"session_id": small["session_id"], "ack_voice": True})
        self.assertEqual(done["status"], "executed")

        # 3 大额触发子女远程授权
        big = self.post("/agent/plan", {"text": "畀我个女转五千蚊"})
        self.assertEqual(big["action"], "await_auth")
        waiting = self.post("/agent/confirm", {"session_id": big["session_id"],
                                               "ack_voice": True, "ack_popup": True})
        self.assertEqual(waiting["status"], "pending_auth")
        ticket_id = waiting["ticket_id"]
        pending = self.get("/auth/pending")
        self.assertTrue(any(t["id"] == ticket_id for t in pending["tickets"]))
        self.assertTrue(any(n["type"] == "auth_request" for n in pending["notifications"]))
        approved = self.post("/auth/approve", {"ticket_id": ticket_id, "approver": "张伟"})
        self.assertEqual(approved["ticket"]["status"], "approved")
        executed = self.post("/agent/execute", {"session_id": big["session_id"]})
        self.assertEqual(executed["status"], "executed")

        # 4 诈骗话术被拦截并紧急止付
        fraud = self.post("/agent/plan", {"text": "给安全账户转三万八", "hour": 15})
        self.assertEqual(fraud["status"], "blocked")
        stop = self.post("/mock-bank/emergency-stop", {"reason": "我可能被骗了"})
        self.assertIn("96110", stop["hotline"])

        # 5 扣费哨兵
        self.post("/system/reset")
        subs = self.get("/mock-bank/subscriptions")
        self.assertEqual(subs["suspicious_count"], 1)
        bad = next(s for s in subs["items"] if s["suspicious"] and s["status"] == "active")
        cancelled = self.post("/mock-bank/subscriptions/cancel",
                              {"subscription_id": bad["id"]})
        self.assertIn("已取消", cancelled["summary"])

        # 6 守护日历生日联动
        due = self.get("/guard/events?today=2026-10-14")
        self.assertEqual([e["id"] for e in due["due"]], ["G-生日"])
        chain = self.post("/guard/run?event_id=G-%E7%94%9F%E6%97%A5")
        self.assertEqual(chain["event"], "老伴王建国生日")
        self.assertGreaterEqual(len(chain["steps"]), 3)

    def test_05_full_attack_sweep(self):
        scripts = self.get("/demo/scripts")["scripts"]
        self.assertGreaterEqual(len(scripts), 12)
        for meta in scripts:
            with self.subTest(script=meta["id"]):
                r = self.post("/demo/attack", {"script_id": meta["id"]})
                self.assertGreater(r["vulnerable"]["lost"], 0)
                self.assertEqual(r["defended"]["lost"], 0)

    def test_06_audit_trail_is_complete(self):
        self.post("/system/reset")
        s = self.post("/agent/plan", {"text": "给老李转五十块"})
        self.post("/agent/confirm", {"session_id": s["session_id"], "ack_voice": True})
        stages = {e["stage"] for e in self.get("/audit?limit=100")["entries"]}
        self.assertTrue({"intent", "plan", "risk", "confirm", "execute"} <= stages)

    def test_07_ticket_replay_is_rejected(self):
        self.post("/system/reset")
        big = self.post("/agent/plan", {"text": "给我女儿转五千块"})
        w = self.post("/agent/confirm", {"session_id": big["session_id"],
                                         "ack_voice": True, "ack_popup": True})
        self.post("/auth/approve", {"ticket_id": w["ticket_id"], "approver": "张伟"})
        self.post("/agent/execute", {"session_id": big["session_id"]})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/auth/approve", {"ticket_id": w["ticket_id"], "approver": "张伟"})
        self.assertEqual(ctx.exception.code, 409)

    def test_08_wealth_purchase_notifies_child(self):
        """老人端买理财，子女端必须收到通知。"""
        self.post("/system/reset")
        p = self.post("/agent/plan", {"text": "帮我买一万块的理财，要稳当的", "hour": 10})
        self.assertEqual(p["plan"]["steps"][0]["tool"], "wealth.purchase")
        c = self.post("/agent/confirm", {"session_id": p["session_id"],
                                         "ack_voice": True, "ack_popup": True})
        self.assertEqual(c["status"], "executed")
        notes = self.get("/auth/pending")["notifications"]
        hit = [n for n in notes if n["type"] == "wealth_purchase"]
        self.assertTrue(hit, f"子女端没有收到理财申购通知：{notes}")
        self.assertIn("理财产品", hit[0]["title"])

    def test_09_blocked_operation_notifies_child(self):
        self.post("/system/reset")
        self.post("/agent/plan", {"text": "给安全账户转三万八", "hour": 15})
        notes = self.get("/auth/pending")["notifications"]
        hit = [n for n in notes if n["type"] == "blocked"]
        self.assertTrue(hit, f"子女端没有收到风控拦截通知：{notes}")
        self.assertIn("风控", hit[0]["title"])

    def test_10_card_loss_notifies_child(self):
        self.post("/system/reset")
        p = self.post("/agent/plan", {"text": "我的卡丢了，赶紧挂失", "hour": 10})
        c = self.post("/agent/confirm", {"session_id": p["session_id"],
                                         "ack_voice": True, "ack_popup": True})
        self.assertEqual(c["status"], "executed")
        notes = self.get("/auth/pending")["notifications"]
        self.assertTrue(any(n["type"] == "card_loss" for n in notes))

    def test_11_balance_reflects_every_transfer(self):
        """连续转账后，账户余额必须每次都更新（不能滞后一笔）。"""
        self.post("/system/reset")
        before = self.get("/system/status")["elder"]["balance"]
        for i in range(1, 4):
            p = self.post("/agent/plan", {"text": "给老李转五十块", "hour": 10})
            self.post("/agent/confirm", {"session_id": p["session_id"], "ack_voice": True})
            after = self.get("/system/status")["elder"]["balance"]
            self.assertAlmostEqual(after, before - 50 * i, places=2,
                                   msg=f"第 {i} 次转账后余额没有同步")

    def test_12_multi_turn_slot_filling_over_http(self):
        self.post("/system/reset")
        p = self.post("/agent/plan", {"text": "给老李转", "hour": 10})
        self.assertEqual(p["status"], "clarify")
        q = self.post("/agent/plan", {"text": "五百块", "hour": 10,
                                      "session_id": p["session_id"]})
        self.assertEqual(q["status"], "pending_confirm")
        self.assertEqual(q["plan"]["steps"][0]["params"]["amount"], 500.0)
        # 500 元超过免密额度，属于 L1，需要弹窗确认
        c = self.post("/agent/confirm", {"session_id": q["session_id"],
                                         "ack_voice": True, "ack_popup": True})
        self.assertEqual(c["status"], "executed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
