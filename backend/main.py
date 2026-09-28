"""FastAPI 入口：语音进出、智能体编排、风控、授权票据、模拟银行、攻防演示台。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import agent, attacks, bank, nlu, risk, voice
from .schemas import (AsrRequest, AttackRequest, AuthApproveRequest, ConfirmRequest,
                      DeployPlanRequest, EmergencyRequest, ExecuteRequest, PlanRequest,
                      RiskRequest, SubscriptionCancelRequest, TtsRequest)

app = FastAPI(title="老年人语音数字银行管家", version="1.0.0",
              description="语音优先交互 + 权限分级 + 幻觉/注入双防御的适老银行智能体")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


# --------------------------------------------------------------------------
# 语音
# --------------------------------------------------------------------------


@app.post("/asr", tags=["语音"])
def asr(req: AsrRequest):
    return voice.transcribe(req)


@app.post("/tts", tags=["语音"])
def tts(req: TtsRequest):
    return voice.synthesize(req)


@app.get("/asr/fallback-lines", tags=["语音"])
def fallback_lines():
    return {"lines": voice.fallback_lines()}


# --------------------------------------------------------------------------
# 智能体
# --------------------------------------------------------------------------


async def _push_child(payload: dict) -> None:
    """把需要子女知情的事情推送到子女端（WebSocket + 通知列表）。

    两类：一是风控直接拦下的操作，二是老人自主完成、但子女应当知情的动作
    （理财申购 / 赎回、挂失、办卡、守护日历联动、定期转账）。
    """
    session_id = payload.get("session_id")
    plan = payload.get("plan") or {}
    steps = plan.get("steps") or []
    tool = steps[0]["tool"] if steps else None
    status = payload.get("status")

    if status == "blocked":
        risk = payload.get("risk") or {}
        reasons = risk.get("reasons") or []
        await agent.notify({
            "type": "blocked",
            "title": "一笔操作被风控拦下",
            "detail": "；".join(reasons)[:150] or "命中风险规则",
            "level": risk.get("level"),
            "reasons": reasons,
        })
        return

    if status != "executed" or tool not in agent.CHILD_NOTIFY_TOOLS:
        return
    if session_id and session_id in agent.NOTIFIED_SESSIONS:
        return
    if session_id:
        agent.NOTIFIED_SESSIONS.add(session_id)
    result = payload.get("result") or {}
    summary = (result.get("steps") or [{}])[0].get("summary", "")
    await agent.notify({
        "type": tool.replace(".", "_"),
        "title": agent.CHILD_NOTIFY_TOOLS[tool],
        "detail": summary or (steps[0].get("summary", "") if steps else ""),
    })


@app.post("/agent/plan", tags=["智能体"])
async def plan(req: PlanRequest):
    payload = agent.create_session(req)
    if payload.get("status") == "blocked":
        await _push_child(payload)
    return payload


@app.post("/agent/confirm", tags=["智能体"])
async def confirm(req: ConfirmRequest):
    result = agent.confirm(req.session_id, req)
    if result.get("ticket_id") and result.get("status") == "pending_auth":
        session = agent.SESSIONS[req.session_id]
        ticket = agent.TICKETS[result["ticket_id"]]
        await agent.notify({
            "type": "auth_request",
            "title": "老人发起一笔大额转账，等您授权",
            "detail": f"¥{ticket.amount:,.2f} → {ticket.payee}",
            "ticket": ticket.model_dump(mode="json"),
            "elder_text": session["plan"].readback,
            "reasons": session["risk"].reasons,
        })
    else:
        await _push_child(result)
    return result


@app.post("/agent/execute", tags=["智能体"])
async def execute(req: ExecuteRequest):
    agent.expire_stale_tickets()
    result = agent.execute(req.session_id)
    await _push_child(result)
    return result


# --------------------------------------------------------------------------
# 风控
# --------------------------------------------------------------------------


@app.post("/risk/evaluate", tags=["风控"])
def evaluate(req: RiskRequest):
    return risk.evaluate(
        amount=req.amount, payee=req.payee, hour=req.hour,
        device_trusted=req.device_trusted, voiceprint_ok=req.voiceprint_ok,
        second_speaker=req.second_speaker, hesitation_count=req.hesitation_count,
        utterance=req.utterance, defense_enabled=req.defense_enabled,
    )


# --------------------------------------------------------------------------
# 授权票据
# --------------------------------------------------------------------------


@app.get("/auth/pending", tags=["授权"])
def pending_tickets():
    agent.expire_stale_tickets()
    return {"tickets": [t.model_dump(mode="json") for t in agent.TICKETS.values()],
            "notifications": agent.NOTIFICATIONS[-20:]}


@app.post("/auth/approve", tags=["授权"])
def approve(req: AuthApproveRequest):
    result = agent.approve_ticket(req)
    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@app.get("/auth/ticket/{ticket_id}", tags=["授权"])
def get_ticket(ticket_id: str):
    agent.expire_stale_tickets()
    ticket = agent.TICKETS.get(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="票据不存在")
    return ticket.model_dump(mode="json")


@app.websocket("/ws/auth")
async def ws_auth(ws: WebSocket):
    await ws.accept()
    agent.attach_ws(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        agent.detach_ws(ws)


# --------------------------------------------------------------------------
# 模拟银行核心
# --------------------------------------------------------------------------


@app.get("/mock-bank/accounts/{account_id}", tags=["模拟银行"])
def get_account(account_id: str):
    acc = bank.ACCOUNTS.get(account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="账户不存在")
    return acc


@app.get("/mock-bank/accounts/{account_id}/transactions", tags=["模拟银行"])
def get_transactions(account_id: str):
    if account_id not in bank.ACCOUNTS:
        raise HTTPException(status_code=404, detail="账户不存在")
    return {"transactions": bank.TRANSACTIONS}


@app.get("/mock-bank/bill-report", tags=["模拟银行"])
def get_bill_report(period: str = "month"):
    """period=month 月度报告；period=year 年度报告。"""
    return agent.bill_report("year" if period == "year" else "month")


@app.get("/mock-bank/schedules", tags=["模拟银行"])
def get_schedules():
    return {"schedules": bank.SCHEDULES}


@app.get("/mock-bank/wealth-products", tags=["模拟银行"])
def get_wealth_products():
    return {"products": bank.WEALTH_PRODUCTS, "holdings": bank.HOLDINGS,
            "elder_risk_band": ["R1", "R2"]}


@app.get("/mock-bank/cards", tags=["模拟银行"])
def get_cards():
    return {"cards": bank.CARDS, "limits": bank.LIMITS["E001"],
            "applications": bank.CARD_APPLICATIONS}


@app.post("/mock-bank/limits", tags=["模拟银行"])
def set_limits(free_limit: float | None = None, single_limit: float | None = None):
    result = agent.update_limits(free_limit=free_limit, single_limit=single_limit)
    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@app.post("/mock-bank/proxy", tags=["模拟银行"])
def proxy(kind: str, target: str, amount: float = 0.0):
    """kind=wealth 代购低风险理财；kind=bill 代办固定支出缴费。"""
    result = agent.proxy_action(kind, target, amount)
    if "error" in result:
        raise HTTPException(status_code=409, detail=result)
    return result


@app.post("/mock-bank/cards/{card_id}/loss", tags=["模拟银行"])
def report_loss(card_id: str):
    card = next((c for c in bank.CARDS if c["id"] == card_id), None)
    if card is None:
        raise HTTPException(status_code=404, detail="卡片不存在")
    return agent._execute_step(
        agent.PlanStep(tool="card.loss", params={"card_id": card_id}), {})


@app.post("/mock-bank/cards/{card_id}/lock", tags=["模拟银行"])
def lock_card(card_id: str, feature: str = "overseas", enable: bool = False):
    card = next((c for c in bank.CARDS if c["id"] == card_id), None)
    if card is None:
        raise HTTPException(status_code=404, detail="卡片不存在")
    return agent._execute_step(
        agent.PlanStep(tool="card.switch",
                       params={"card_id": card_id, "feature": feature, "enable": enable}), {})


@app.get("/mock-bank/subscriptions", tags=["模拟银行"])
def get_subscriptions():
    return agent.scan_subscriptions()


@app.post("/mock-bank/subscriptions/cancel", tags=["模拟银行"])
def cancel_subscription(req: SubscriptionCancelRequest):
    sub = next((s for s in bank.SUBSCRIPTIONS if s["id"] == req.subscription_id), None)
    if sub is None:
        raise HTTPException(status_code=404, detail="订阅不存在")
    return agent._execute_step(
        agent.PlanStep(tool="subscription.cancel",
                       params={"subscription_id": req.subscription_id}), {})


@app.post("/mock-bank/emergency-stop", tags=["模拟银行"])
async def emergency(req: EmergencyRequest):
    result = agent.emergency_stop(req.reason)
    await agent.notify({
        "type": "emergency_stop",
        "title": "老人触发了紧急止付",
        "detail": f"原因：{req.reason}　已冻结全部支付渠道并引导 96110",
        "reason": req.reason,
        "report": result,
    })
    return result


# --------------------------------------------------------------------------
# 守护日历
# --------------------------------------------------------------------------


@app.get("/guard/events", tags=["守护日历"])
def guard_events(today: str | None = None):
    return {"events": bank.GUARD_EVENTS, "due": agent.due_guard_events(today)}


@app.post("/guard/run", tags=["守护日历"])
def guard_run(event_id: str, today: str | None = None):
    event = next((e for e in bank.GUARD_EVENTS if e["id"] == event_id), None)
    if event is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    return agent.run_guard_event(event_id, {})


@app.post("/guard/deploy", tags=["守护日历"])
def guard_deploy(req: DeployPlanRequest):
    from datetime import date as _date
    event_date = req.date or _date.today().isoformat()
    event = {
        "id": f"G-{req.event}", "title": req.event, "date": event_date,
        "advance_days": req.advance_days, "kind": "child_deployed",
        "deployed_by": "张伟", "lock_amount": req.lock_amount, "actions": req.actions,
        "steps": [],
    }
    bank.GUARD_EVENTS.append(event)
    agent.audit("auth", "张伟", f"子女端远程预置守护日历：{req.event}", **event)
    return {"event": event}


# --------------------------------------------------------------------------
# 攻防演示台
# --------------------------------------------------------------------------


@app.get("/demo/scripts", tags=["攻防演示"])
def demo_scripts():
    return {"scripts": attacks.list_scripts()}


@app.post("/demo/attack", tags=["攻防演示"])
def demo_attack(req: AttackRequest):
    return attacks.run_script(req.script_id)


# --------------------------------------------------------------------------
# 审计与系统
# --------------------------------------------------------------------------


@app.get("/audit", tags=["审计"])
def get_audit(limit: int = 100):
    return {"entries": [e.model_dump(mode="json") for e in agent.AUDIT[-limit:]]}


@app.get("/system/status", tags=["系统"])
def system_status():
    llm_live = nlu.llm_available()
    cloud_asr = voice.stt_available()
    cloud_tts = voice.tts_available()
    return {
        # 意图理解：系统的 AI 核心，是否走高模型
        "llm_provider": "qwen(通义千问)" if llm_live else "rule-engine-fallback",
        "llm_live": llm_live,
        # /asr 接口可用的通道 vs 随仓库的 Web 界面实际走的通道
        "asr_provider": "xfyun-iat" if cloud_asr else "browser-native-stt",
        "asr_in_use": "browser-native-stt",
        "tts_provider": "xfyun-tts" if cloud_tts else "browser-speech-synthesis",
        # 只表示"意图理解是否降级为规则解析器"，此前把它写成 LLM 与 ASR 都可用才为 false，
        # 而浏览器界面并不经过云端 ASR，导致配上大模型后这个字段仍然是 true，容易让人误判。
        "degraded_mode": not llm_live,
        "ai_stack": {
            "意图理解": "通义千问" if llm_live else "本地规则解析器（降级）",
            "任务规划": "Plan-and-Execute",
            "幻觉拦截": "槽位一致性校验",
            "资金风控": "确定性规则引擎 L0–L3",
            "语音识别": "浏览器原生识别",
            "语音合成": "讯飞在线合成" if cloud_tts else "浏览器语音合成",
        },
        "elder": bank.ACCOUNTS["E001"],
        "limits": bank.LIMITS["E001"],
        "auth_threshold": risk.AUTH_THRESHOLD,
    }


@app.post("/system/reset", tags=["系统"])
def system_reset():
    agent.reset_all()
    return {"ok": True, "message": "演示数据已重置"}


# --------------------------------------------------------------------------
# 前端
# --------------------------------------------------------------------------


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")
