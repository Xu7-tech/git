"""智能体编排：意图 → 计划（Plan-and-Execute）→ 风控 → 复述确认 → 执行。

设计约束：
* 权限分级一律由 risk.py 的规则引擎裁定，LLM 无权放行。
* 复述内容与最终执行参数做一致性校验，不一致即中止。
* 每个阶段都写审计日志，任何一笔资金操作都能完整回放。
"""
from __future__ import annotations

import itertools
import re
import uuid
from datetime import datetime, timedelta

from . import bank, nlu, risk
from .schemas import (AuditEntry, AuthTicket, Intent, Plan, PlanStep, RiskDecision, RiskLevel)

AUDIT: list[AuditEntry] = []
SESSIONS: dict[str, dict] = {}
TICKETS: dict[str, AuthTicket] = {}
NOTIFICATIONS: list[dict] = []
WS_CLIENTS: list = []                 # 由 main.py 注册的 WebSocket 连接
NOTIFIED_SESSIONS: set[str] = set()   # 已推送过执行通知的会话，避免重复推送
_SEQ = itertools.count(1)

# 这些动作不会拦下老人，但子女应当知情 —— 执行后主动推送到子女端
CHILD_NOTIFY_TOOLS = {
    "wealth.purchase": "老人申购了理财产品",
    "wealth.redeem": "老人赎回了理财",
    "card.loss": "老人挂失了银行卡",
    "card.apply": "老人提交了办卡申请",
    "guard.run": "守护日历动作链已执行",
    "transfer.schedule": "老人设置了定期转账",
}


def audit(stage: str, actor: str, note: str, **detail) -> AuditEntry:
    entry = AuditEntry(ts=datetime.now(), stage=stage, actor=actor, note=note, detail=detail)
    AUDIT.append(entry)
    del AUDIT[:-500]
    return entry


def attach_ws(client) -> None:
    WS_CLIENTS.append(client)


def detach_ws(client) -> None:
    if client in WS_CLIENTS:
        WS_CLIENTS.remove(client)


async def notify(payload: dict) -> None:
    NOTIFICATIONS.append({"ts": datetime.now().isoformat(timespec="seconds"), **payload})
    del NOTIFICATIONS[:-200]
    for client in list(WS_CLIENTS):
        try:
            await client.send_json(payload)
        except Exception:  # 连接已断，忽略即可
            detach_ws(client)


def money(value: float) -> str:
    return f"{value:,.2f}"


# --------------------------------------------------------------------------
# 计划生成
# --------------------------------------------------------------------------

REMARK_RE = re.compile(r"备注(?:是|写|填)?[:：]?(.+)$")


def _resolve_payee(slots: dict) -> tuple[str | None, list[str]]:
    """把"孙子"这类口语别名解析成具体人名；返回 (收款人, 歧义候选)。"""
    names = slots.get("payee_hits") or []
    if len(names) == 1:
        return names[0], []
    if len(names) > 1:
        return None, names
    candidates: list[str] = []
    for alias in slots.get("alias_hits") or []:
        for name in bank.ALIASES.get(alias, []):
            if name not in candidates:
                candidates.append(name)
    if len(candidates) == 1:
        return candidates[0], []
    if len(candidates) > 1:
        return None, candidates
    return None, []


def _payee_label(name: str) -> str:
    note = nlu.RELATION_NOTES.get(name)
    return f"{name}（{note}）" if note else name


def _merge_slots(base: dict, extra: dict) -> dict:
    """把新提供的槽位并进原有槽位；空值不覆盖已有信息。"""
    merged = dict(base)
    for key, value in extra.items():
        if key in ("alias_hits", "payee_hits"):
            if value:
                merged[key] = value
        elif value not in (None, 0, 0.0, "", []):
            merged[key] = value
    return merged


def _pending_slots(text: str) -> dict:
    """从一句「只有答案、没有动词」的话里抽出可用槽位，例如「五百块」「王小龙」。"""
    slots: dict = {}
    # 先按常规找金额；找不到再按「整句就是一个金额」试一次，
    # 因为老人回答「多少钱」时常说「一千五」这种不带量词的说法。
    amount = nlu.extract_amount(text) or nlu.parse_bare_amount(text)
    if amount:
        slots["amount"] = amount
    aliases, names = nlu.find_payees(text)
    if aliases or names:
        slots["alias_hits"], slots["payee_hits"] = aliases, names
    total, people = nlu.parse_split(text)
    if total:
        slots["total"] = total
    if people:
        slots["people"] = people
    return slots


def _merge_with_pending(prev: Intent, text: str) -> Intent | None:
    """把这一句当作上一轮缺失信息的回答，合并成一个完整意图。

    判定顺序很关键：先看这一句自己能不能独立构成指令。
    能独立成立且换了意图，说明老人改口说了一件新事，不能硬套进上一轮；
    只有它自己立不住（「五百块」「王小龙」）时，才作为补槽答案。
    """
    fresh = nlu.parse_intent_rule(text, prev.dialect)
    if fresh.name == prev.name:
        merged = _merge_slots(prev.slots, fresh.slots)
    elif fresh.name == "unknown":
        merged = _merge_slots(prev.slots, _pending_slots(text))
    else:
        return None
    if merged == prev.slots or not merged:
        return None                     # 这一句没带来任何新信息
    merged = {k: v for k, v in merged.items() if k != "consistency_note"}
    if prev.name == "bill_split":
        total, people = merged.get("total"), merged.get("people")
        if total and people:
            merged["amount"] = round(total / people, 2)
    return Intent(name=prev.name, slots=merged,
                  confidence=max(prev.confidence, fresh.confidence),
                  raw_utterance=f"{prev.raw_utterance}｜{text}",
                  speaker=prev.speaker, dialect=prev.dialect, source="rule")


def _resolve_intents(req) -> tuple[list[Intent], dict]:
    """决定这一句是「补上一轮缺的信息」还是「一条新指令」，可能拆出多件事。"""
    prev_session = SESSIONS.get(req.session_id) if getattr(req, "session_id", None) else None
    if prev_session and prev_session["status"] == "clarify":
        merged = _merge_with_pending(prev_session["plan"].intent, req.text)
        if merged is not None:
            audit("intent", req.speaker,
                  f"承接上一轮追问，补入缺失信息后仍是 {merged.name}",
                  utterance=merged.raw_utterance, slots=merged.slots)
            return [merged], {}
    return nlu.parse_intents(req.text, req.dialect)


def build_plan(intent: Intent) -> Plan:
    name = intent.name
    slots = intent.slots
    raw = intent.raw_utterance

    if name == "transfer_schedule":
        payee, options = _resolve_payee(slots)
        if options:
            return Plan(intent=intent, steps=[PlanStep(
                tool="contact.disambiguate", params={"options": options},
                summary="名字有歧义，需要老人确认具体是哪一位")],
                readback="这个名字对应好几位联系人，我需要您确认一下。")
        if not payee:
            return Plan(intent=intent, steps=[PlanStep(
                tool="contact.ask", params={}, summary="没听清收款人")],
                readback="您想定期转给谁？可以说名字，也可以说关系。")
        amount = slots.get("amount") or 0.0
        if amount <= 0:
            return Plan(intent=intent, steps=[PlanStep(
                tool="contact.ask_amount", params={"payee": payee}, summary="缺少金额")],
                readback=f"每个月要转给{_payee_label(payee)}多少钱？")
        day = int(slots.get("day") or 1)
        step = PlanStep(tool="transfer.schedule",
                        params={"payee": payee, "amount": amount, "day": day},
                        summary=f"每月 {day} 号向 {payee} 转 {money(amount)} 元")
        return Plan(intent=intent, steps=[step],
                    readback=f"以后每个月 {day} 号，我都会自动给{_payee_label(payee)}"
                             f"转 {money(amount)} 元。确认吗？")

    if name == "bill_split":
        payee, options = _resolve_payee(slots)
        total = slots.get("total") or 0.0
        people = int(slots.get("people") or 0)
        share = slots.get("amount") or 0.0
        if not total or not people:
            return Plan(intent=intent, steps=[PlanStep(
                tool="contact.ask_split", params={}, summary="缺少总额或人数")],
                readback="您告诉我这次一共多少钱、几个人分，我帮您算自己那份。")
        if not payee:
            return Plan(intent=intent, steps=[PlanStep(
                tool="contact.ask", params={}, summary="没听清收款人")],
                readback=f"这一份 {money(share)} 元要付给谁？")
        step = PlanStep(tool="transfer.execute",
                        params={"payee": payee, "amount": share,
                                "remark": f"拼单分摊 {people} 人"},
                        summary=f"向 {payee} 代付拼单分摊款 {money(share)} 元")
        return Plan(intent=intent, steps=[step],
                    readback=f"这次一共 {money(total)} 元、{people} 个人分，"
                             f"您这一份是 {money(share)} 元，付款对象是{_payee_label(payee)}。确认吗？")

    if name == "transfer":
        payee, options = _resolve_payee(slots)
        if options:
            return Plan(intent=intent, steps=[PlanStep(
                tool="contact.disambiguate", params={"options": options},
                summary="名字有歧义，需要老人确认具体是哪一位")],
                readback="这个名字对应好几位联系人，我需要您确认一下。",
                risk_hint="歧义澄清")
        if not payee:
            return Plan(intent=intent, steps=[PlanStep(
                tool="contact.ask", params={}, summary="没听清收款人")],
                readback="您想转给谁？可以说名字，也可以说关系，比如「孙子」「老李」。")
        amount = slots.get("amount") or 0.0
        if amount <= 0:
            return Plan(intent=intent, steps=[PlanStep(
                tool="contact.ask_amount", params={"payee": payee}, summary="缺少金额")],
                readback=f"您要转给{_payee_label(payee)}多少钱？说个数字就行，比如「五百块」。")
        remark_match = REMARK_RE.search(raw)
        remark = remark_match.group(1).strip() if remark_match else ""
        step = PlanStep(tool="transfer.execute",
                        params={"payee": payee, "amount": amount, "remark": remark},
                        summary=f"向 {payee} 转出 {money(amount)} 元")
        info = bank.payee_info(payee) or {}
        known = info.get("known_days", 0)
        readback = (
            f"您要转出 {money(amount)} 元，收款人是{_payee_label(payee)}，"
            f"您和他认识 {known} 天{'，这是您的常用收款人' if info.get('whitelisted') else ''}。"
            + (f"备注写的是「{remark}」。" if remark else "")
            + "确认无误就说「确认」。"
        )
        return Plan(intent=intent, steps=[step], readback=readback)

    if name == "balance_query":
        acc = bank.ACCOUNTS["E001"]
        readback = (f"您的活期账户余额是 {money(acc['balance'])} 元，"
                    f"其中 {money(acc['locked'])} 元被定为专项用途暂不可用。")
        return Plan(intent=intent, steps=[PlanStep(tool="bank.balance", params={},
                                                   summary="查询余额")], readback=readback)

    if name == "bill_analysis":
        period = slots.get("period", "month")
        report = bill_report(period)
        readback = report["speech"]
        label = "月度" if period == "month" else "年度"
        return Plan(intent=intent, steps=[PlanStep(tool="bank.bill_report",
                                                   params={"period": period},
                                                   summary=f"生成{label}资金安全体检报告")],
                    readback=readback)

    if name == "wealth_purchase":
        hint = "稳当" if any(k in raw for k in ("稳当", "穩陣", "稳健", "穩健", "保本")) else ""
        products = [p for p in bank.WEALTH_PRODUCTS if p["elder_ok"] or p.get("suspicious")]
        amount = slots.get("amount") or 0.0
        # 老人点名要买某只产品时以点名为准，再交给适当性闸门判定。
        named = next((p for p in bank.WEALTH_PRODUCTS if p["name"] in raw), None)
        if amount <= 0:
            return Plan(intent=intent, steps=[PlanStep(
                tool="contact.ask_amount", params={"payee": "理财"}, summary="缺少金额")],
                readback="您要买多少钱的？说个数字就行，比如「一万块」。")
        top = [named] if named else [p for p in products if p["elder_ok"]][:2]
        step = PlanStep(tool="wealth.purchase",
                        params={"product_id": top[0]["id"], "amount": amount},
                        summary=f"申购 {top[0]['name']} {money(amount)} 元")
        if named:
            readback = (f"您点名的是「{named['name']}」，风险等级 {named['risk']}，"
                        f"参考年化 {named['annual']}%。金额 {money(amount)} 元，我先帮您核一下再说。")
        else:
            readback = ("我按您能承受的风险挑了两个稳当的："
                        + "；".join(f"{p['name']}，风险等级 {p['risk']}，参考年化 {p['annual']}%"
                                   for p in top)
                        + f"。您是要买第一个吗？金额 {money(amount)} 元。")
        return Plan(intent=intent, steps=[step], readback=readback, risk_hint=hint)

    if name == "wealth_redeem":
        holdings = bank.HOLDINGS
        total = sum(h["amount"] for h in holdings)
        step = PlanStep(tool="wealth.redeem", params={"holding_ids": [h["id"] for h in holdings]},
                        summary=f"赎回持仓共 {money(total)} 元")
        return Plan(intent=intent, steps=[step],
                    readback=f"您持有的稳健理财一共 {money(total)} 元，全部赎回吗？确认后钱会回到活期。")

    if name == "card_loss":
        card = bank.CARDS[0]
        step = PlanStep(tool="card.loss", params={"card_id": card["id"]},
                        summary=f"挂失尾号 {card['tail']} 的借记卡")
        return Plan(intent=intent, steps=[step],
                    readback=f"我现在就给您挂失尾号 {card['tail']} 的借记卡，挂失后这张卡不能再用，补卡会寄到您家。确认吗？")

    if name == "card_apply":
        card_type = "信用卡" if "信用" in raw else "借记卡"
        step = PlanStep(tool="card.apply", params={"card_type": card_type},
                        summary=f"提交一张{card_type}申请")
        return Plan(intent=intent, steps=[step],
                    readback=f"我帮您提交一张{card_type}的申请。进度我会随时告诉您，"
                             f"收到新卡后还要本人激活才能用。确认吗？")

    if name == "card_switch":
        enable = slots.get("enable", False)
        feature = slots.get("feature", "overseas")
        label = "境外交易" if feature == "overseas" else "境内交易"
        step = PlanStep(tool="card.switch",
                        params={"card_id": bank.CARDS[0]["id"], "feature": feature, "enable": enable},
                        summary=f"{'打开' if enable else '关闭'}{label}")
        return Plan(intent=intent, steps=[step],
                    readback=f"您要把{label}{'打开' if enable else '关掉'}，确认吗？")

    if name in ("subscription_scan", "subscription_cancel"):
        scan = scan_subscriptions()
        if name == "subscription_cancel" or slots.get("want_cancel"):
            target = next((s for s in scan["items"] if s["suspicious"]), None)
            if target is None:
                return Plan(intent=intent, steps=[PlanStep(tool="subscription.list", params={},
                                                           summary="列出订阅")],
                            readback="您目前没有查出可疑的自动扣费。")
            step = PlanStep(tool="subscription.cancel", params={"subscription_id": target["id"]},
                            summary=f"取消 {target['merchant']} 的每月 {money(target['monthly_amount'])} 元扣费")
            return Plan(intent=intent, steps=[step],
                        readback=f"我给您取消「{target['merchant']}」，它每月扣 {money(target['monthly_amount'])} 元。确认吗？")
        return Plan(intent=intent, steps=[PlanStep(tool="subscription.list", params={},
                                                   summary="扫描订阅扣费")],
                    readback=scan["speech"])

    if name == "calendar_query":
        events = due_guard_events()
        if not events:
            upcoming = sorted(bank.GUARD_EVENTS, key=lambda e: e["date"])[:3]
            listing = "；".join(f"{e['date']} {e['title']}" for e in upcoming)
            return Plan(intent=intent, steps=[PlanStep(tool="guard.list", params={},
                                                       summary="查询守护日历")],
                        readback=f"这几天没有要到期的安排。接下来有：{listing}。到了日子我会提前提醒您。")
        ev = events[0]
        step = PlanStep(tool="guard.run", params={"event_id": ev["id"]}, summary=ev["title"])
        return Plan(intent=intent, steps=[step],
                    readback=f"守护日历上，「{ev['title']}」要到了。{'；'.join(ev['actions'])}。要我现在就去办吗？")

    if name == "guard_run":
        target = slots.get("target") or ""
        matched = [e for e in bank.GUARD_EVENTS if target and target in e["title"]]
        event = matched[0] if matched else min(bank.GUARD_EVENTS, key=lambda e: e["date"])
        step = PlanStep(tool="guard.run", params={"event_id": event["id"]},
                        summary=event["title"])
        return Plan(intent=intent, steps=[step],
                    readback=f"守护日历上「{event['title']}」的动作链是：{'；'.join(event['actions'])}。"
                             f"要我现在就去办吗？")

    if name == "emergency_stop":
        step = PlanStep(tool="bank.emergency_stop", params={"reason": raw},
                        summary="冻结全部支付渠道")
        return Plan(intent=intent, steps=[step],
                    readback="我马上给您冻结所有支付渠道，谁都不能再把钱转出去。确认吗？")

    return Plan(intent=intent, steps=[], readback="我没太听明白，您能再说一次吗？比如「给女儿转 500 块」。")


# 会动资金的工具。复合指令里最多只能有一个 —— 一张授权票据只绑一笔钱。
FUND_TOOLS = {"transfer.execute", "transfer.schedule", "wealth.purchase", "wealth.redeem"}
MAX_READONLY_STEPS = 2


def _fund_step(plan: Plan) -> PlanStep | None:
    return next((s for s in plan.steps if s.tool in FUND_TOOLS), None)


def build_multi_plan(intents: list[Intent]) -> Plan:
    """把一句话里的多件事合并成一个计划。

    上限：1 个动资金步骤 + 2 个只读步骤。超出则只办第一件并说明，
    避免出现"一次确认背后藏着多张授权票据"这种说不清的交互。
    """
    plans = [build_plan(i) for i in intents]
    for single in plans:
        # 有任何一件需要先澄清，就只办那一件，别把问句夹进复述里
        if not single.steps or single.steps[0].tool.startswith("contact."):
            return single
    fund = [p for p in plans if p.steps[0].tool in FUND_TOOLS]
    readonly = [p for p in plans if p.steps[0].tool not in FUND_TOOLS][:MAX_READONLY_STEPS]
    dropped = len(fund) > 1 or len(plans) - len(fund) > MAX_READONLY_STEPS
    chosen = fund[:1] + readonly
    if len(chosen) == 1:
        if dropped:
            chosen[0].readback += "一次只办得了一件动钱的事，其余的您过会儿再说一次。"
        return chosen[0]
    steps = [s for single in chosen for s in single.steps]
    lines = "；".join(f"第{i}件，{s.summary}" for i, s in enumerate(steps, 1))
    readback = f"我准备做{len(steps)}件事：{lines}。"
    readback += "要一起办吗？"
    return Plan(intent=chosen[0].intent, steps=steps, readback=readback)


# --------------------------------------------------------------------------
# 会话主流程
# --------------------------------------------------------------------------


def create_session(req) -> dict:
    intents, signal = _resolve_intents(req)
    intent = intents[0]
    names = "、".join(i.name for i in intents)
    audit("intent", req.speaker, f"识别意图 {names}",
          utterance=intent.raw_utterance, dialect=intent.dialect, source=intent.source,
          slots={k: v for k, v in intent.slots.items() if k != "consistency_note"},
          consistency_note=intent.slots.get("consistency_note"),
          intents=[i.name for i in intents], llm_fraud=signal or None)
    plan = build_multi_plan(intents)
    audit("plan", "agent", plan.readback,
          steps=[s.tool for s in plan.steps], readback=plan.readback)

    session_id = f"S{uuid.uuid4().hex[:8]}"
    session = {
        "id": session_id,
        "intent": intent,
        "plan": plan,
        "status": "pending_confirm",
        "risk": None,
        "ticket_id": None,
        "voice_ack": False,
        "popup_ack": False,
        "hesitation": 0,
        "llm_signal": signal,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "result": None,
    }

    first = plan.steps[0] if plan.steps else None
    if first is None or first.tool.startswith("contact."):
        session["status"] = "clarify"
        SESSIONS[session_id] = session
        payload = _session_payload(session)
        if first is not None and first.tool == "contact.disambiguate":
            payload["options"] = [{"name": n, "note": nlu.RELATION_NOTES.get(n, "")}
                                  for n in first.params["options"]]
            payload["elder_text"] = (
                "这个名字对应好几位联系人："
                + "；".join(f"{o['name']}，{o['note']}" for o in payload["options"])
                + "。您说的是哪一位？")
        payload["action"] = "clarify"
        return payload

    decision = _evaluate_plan(session, req)
    session["risk"] = decision
    SESSIONS[session_id] = session
    audit("risk", "rule-engine", f"判定 {decision.level.value}",
          reasons=decision.reasons, blocked=decision.blocked,
          injection_hits=decision.injection_hits)

    if decision.blocked:
        session["status"] = "blocked"
        session["result"] = {"message": "操作已阻断，未发生任何资金变动"}
        return _session_payload(session)

    return _session_payload(session)


_LEVEL_ORDER = {RiskLevel.L0: 0, RiskLevel.L1: 1, RiskLevel.L2: 2, RiskLevel.L3: 3}


def _evaluate_plan(session: dict, req) -> RiskDecision:
    # 用意图携带的原始话语做话术检测：多轮补槽时它是「上一轮｜这一轮」的合并文本，
    # 所以诈骗话术不会因为被拆成两句话就漏检。
    utterance = session["plan"].intent.raw_utterance or req.text
    # 输入层第一道闸门：任何外部不可信文本先净化，命中指令型注入直接阻断。
    injection_hits: list[str] = []
    if req.defense_enabled:
        for chunk in req.untrusted_context:
            _, hits = risk.sanitize_untrusted(chunk)
            injection_hits.extend(hits)
    if injection_hits:
        audit("risk", "rule-engine", "外部字段命中指令型注入，操作被阻断",
              injection_hits=injection_hits)
        return RiskDecision(
            level=RiskLevel.L3, blocked=True,
            reasons=["外部数据（商户名/账单描述）中出现指令型注入内容："
                     + "、".join(injection_hits) + "。外部内容只作为数据，永不作为指令执行"],
            injection_hits=injection_hits,
        )
    # 复合指令要按最严的那一步定级，不能只看第一步 —— 否则"查个账顺便转五万"就绕过了风控
    decisions = [_evaluate_step(step, req, utterance, session.get("llm_signal"))
                 for step in session["plan"].steps]
    worst = max(decisions, key=lambda d: _LEVEL_ORDER[d.level])
    if len(decisions) == 1:
        return worst
    return RiskDecision(level=worst.level, blocked=worst.blocked,
                        reasons=[r for d in decisions for r in d.reasons],
                        limits=worst.limits, injection_hits=worst.injection_hits)


def _evaluate_step(step: PlanStep, req, utterance: str, llm_signal: dict | None) -> RiskDecision:
    if step.tool == "wealth.purchase":
        product = next(p for p in bank.WEALTH_PRODUCTS if p["id"] == step.params["product_id"])
        return risk.wealth_gate(product)
    if step.tool == "bank.emergency_stop":
        return RiskDecision(level=RiskLevel.L0, reasons=["紧急止付属于保命操作，任何额度都直接执行"])
    if step.tool in ("transfer.execute", "transfer.schedule"):
        return risk.evaluate(
            amount=step.params["amount"], payee=step.params["payee"],
            hour=req.hour, device_trusted=req.device_trusted, voiceprint_ok=req.voiceprint_ok,
            second_speaker=req.second_speaker, utterance=utterance,
            defense_enabled=req.defense_enabled, remark=step.params.get("remark", ""),
            llm_fraud=llm_signal,
        )
    if step.tool in READ_ONLY_TOOLS:
        return RiskDecision(level=RiskLevel.L0,
                            reasons=["只读查询，不涉及资金变动，语音播报即可"])
    return RiskDecision(level=RiskLevel.L1,
                        reasons=["会影响您的账户设置或资金安排，需要大字弹窗二次确认"])


READ_ONLY_TOOLS = {"bank.balance", "bank.bill_report", "subscription.list", "guard.list"}


def confirm(session_id: str, req) -> dict:
    session = SESSIONS.get(session_id)
    if session is None:
        return {"error": "会话不存在或已过期"}
    if session["status"] == "clarify":
        return {"error": "当前还需要您补充信息，请先把问题回答完整"}
    if session["status"] in ("executed", "blocked"):
        return _session_payload(session)

    decision: RiskDecision = session["risk"]
    if req.hesitation_ms > 3000:
        session["hesitation"] += 1
        audit("confirm", "elder", f"复述确认环节出现犹豫（{session['hesitation']} 次）",
              hesitation_ms=req.hesitation_ms)
        if session["hesitation"] >= 2:
            step = _fund_step(session["plan"]) or session["plan"].steps[0]
            upgraded = risk.evaluate(
                amount=step.params.get("amount", 0), payee=step.params.get("payee", ""),
                hesitation_count=session["hesitation"], utterance=session["intent"].raw_utterance,
                llm_fraud=session.get("llm_signal"))
            if upgraded.level.value > decision.level.value:
                session["risk"] = upgraded
                audit("risk", "rule-engine", f"因反复改口自动升权至 {upgraded.level.value}",
                      reasons=upgraded.reasons)
                if upgraded.blocked:
                    session["status"] = "blocked"
        payload = _session_payload(session)
        if session["status"] != "blocked" and session["risk"].level == decision.level:
            payload["elder_text"] = "您好像有点拿不定主意。我再念一遍：" + session["plan"].readback
        return payload

    if not req.ack_voice:
        return {"error": "语音复述尚未确认"}
    session["voice_ack"] = True
    needs_popup = decision.level in (RiskLevel.L1, RiskLevel.L2)
    if needs_popup and not req.ack_popup:
        return {"error": "二次确认弹窗尚未确认"}
    session["popup_ack"] = req.ack_popup
    audit("confirm", "elder", "语音复述确认" + ("+ 大字弹窗二次确认" if needs_popup else ""),
          level=decision.level.value)

    if decision.level == RiskLevel.L2 and session["ticket_id"] is None:
        ticket = create_ticket(session)
        session["ticket_id"] = ticket.id
        session["status"] = "pending_auth"
        return _session_payload(session)

    return _finalize(session)


def execute(session_id: str) -> dict:
    session = SESSIONS.get(session_id)
    if session is None:
        return {"error": "会话不存在或已过期"}
    if session["status"] == "executed":
        return _session_payload(session)
    if session["status"] == "blocked":
        return {"error": "该会话已被风控阻断", "session": _session_payload(session)}
    if session["status"] == "pending_auth":
        ticket = TICKETS.get(session["ticket_id"] or "")
        if ticket is None:
            return {"error": "找不到对应的授权票据"}
        if ticket.status == "expired":
            return {"error": "授权票据已超时作废，请重新发起"}
        if ticket.status == "used":
            return {"error": "授权票据已被使用，不可重放"}
        if ticket.status != "approved":
            return {"error": "尚未获得子女授权"}
        step = session["plan"].steps[0]
        # 票据绑定金额与收款人，参数不符即拒绝执行。
        if abs(ticket.amount - step.params.get("amount", -1)) > 0.001 or \
                ticket.payee != step.params.get("payee"):
            return {"error": "票据绑定的金额或收款人与本次操作不一致"}
        ticket.status = "used"
        ticket.used_at = datetime.now()
        audit("auth", "system", f"校验授权票据 {ticket.id} 通过并核销", ticket=ticket.model_dump(mode="json"))
    elif session["status"] != "pending_confirm":
        return {"error": f"当前状态 {session['status']} 不可执行"}
    return _finalize(session)


def _finalize(session: dict) -> dict:
    results = []
    for step in session["plan"].steps:
        outcome = _execute_step(step, session)
        results.append(outcome)
        audit("execute", "bank-core", outcome["summary"], tool=step.tool, **outcome)
    session["status"] = "executed"
    session["result"] = {"steps": results}
    return _session_payload(session)


def _execute_step(step: PlanStep, session: dict) -> dict:
    tool, params = step.tool, step.params
    acc = bank.ACCOUNTS["E001"]

    if tool == "transfer.execute":
        acc["balance"] -= params["amount"]
        tx = {"id": bank.next_id("T", bank.TRANSACTIONS), "date": bank.today(), "time": datetime.now().strftime("%H:%M"),
              "amount": params["amount"], "direction": "out", "counterparty": params["payee"],
              "category": "转账", "channel": "语音管家", "remark": params.get("remark", "")}
        bank.TRANSACTIONS.insert(0, tx)
        return {"summary": f"已向 {params['payee']} 转出 {money(params['amount'])} 元",
                "tx": tx, "balance": acc["balance"]}

    if tool == "bank.balance":
        return {"summary": "余额查询完成", "balance": acc["balance"], "locked": acc["locked"]}

    if tool == "bank.bill_report":
        report = bill_report(params.get("period", "month"))
        return {"summary": "资金安全体检报告已生成", **report}

    if tool == "transfer.schedule":
        entry = {"id": f"SCH{len(bank.SCHEDULES) + 1:03d}", "payee": params["payee"],
                 "amount": params["amount"], "day": params["day"],
                 "created": bank.today()}
        bank.SCHEDULES.append(entry)
        return {"summary": f"已设置定期转账：每月 {params['day']} 号向 {params['payee']} "
                           f"转 {money(params['amount'])} 元", "schedule": entry}

    if tool == "wealth.purchase":
        product = next(p for p in bank.WEALTH_PRODUCTS if p["id"] == params["product_id"])
        acc["balance"] -= params["amount"]
        bank.HOLDINGS.append({"id": bank.next_id("H", bank.HOLDINGS),
                              "product_id": product["id"], "amount": params["amount"]})
        if product["risk"] not in ("R1", "R2"):
            return {"summary": f"「{product['name']}」已进入 24 小时冷静期，到期前可随时撤回",
                    "cooling_off_hours": 24, "product": product["name"]}
        return {"summary": f"已申购「{product['name']}」{money(params['amount'])} 元",
                "balance": acc["balance"]}

    if tool == "wealth.redeem":
        total = sum(h["amount"] for h in bank.HOLDINGS)
        acc["balance"] += total
        bank.HOLDINGS.clear()
        return {"summary": f"已赎回 {money(total)} 元，资金回到活期", "balance": acc["balance"]}

    if tool == "card.loss":
        card = next(c for c in bank.CARDS if c["id"] == params["card_id"])
        card["loss_reported"] = True
        card["status"] = "已挂失"
        card["replacement"] = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
        return {"summary": f"尾号 {card['tail']} 的卡已挂失，新卡预计 {card['replacement']} 寄到",
                "card": card}

    if tool == "card.apply":
        application = {
            "id": f"APP{len(bank.CARD_APPLICATIONS) + 1:03d}",
            "card_type": params.get("card_type", "借记卡"),
            "status": "已受理",
            "submitted": bank.today(),
            "expected": (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d"),
            "pickup": "可寄到您家，也可以到网点领",
        }
        bank.CARD_APPLICATIONS.append(application)
        return {"summary": f"{application['card_type']}申请已受理（工单 {application['id']}），"
                           f"预计 {application['expected']} 前出结果", "application": application}

    if tool == "card.switch":
        card = next(c for c in bank.CARDS if c["id"] == params["card_id"])
        if params["feature"] == "overseas":
            card["overseas_enabled"] = params["enable"]
        return {"summary": f"{'打开' if params['enable'] else '关闭'}成功", "card": card}

    if tool == "subscription.cancel":
        sub = next(s for s in bank.SUBSCRIPTIONS if s["id"] == params["subscription_id"])
        sub["status"] = "cancelled"
        return {"summary": f"已取消「{sub['merchant']}」的自动扣费", "subscription": sub}

    if tool == "subscription.list":
        return {"summary": "订阅扫描完成", **scan_subscriptions()}

    if tool == "guard.list":
        return {"summary": "守护日历查询完成",
                "events": sorted(bank.GUARD_EVENTS, key=lambda e: e["date"])}

    if tool == "guard.run":
        return run_guard_event(params["event_id"], session)

    if tool == "bank.emergency_stop":
        return emergency_stop(params.get("reason", ""))

    if tool == "notify.reminder":
        return {"summary": step.summary}

    if tool == "wealth.lock_liquid":
        acc["locked"] += params.get("amount", 0.0)
        return {"summary": f"已锁定活期 {money(params.get('amount', 0.0))} 元作为专项预算",
                "locked": acc["locked"]}

    return {"summary": f"未实现的工具 {tool}"}


# --------------------------------------------------------------------------
# 授权票据
# --------------------------------------------------------------------------


def create_ticket(session: dict) -> AuthTicket:
    # 票据只能绑动资金的那一步：复合指令里其余步骤是只读的，没有金额与收款人
    step = _fund_step(session["plan"]) or session["plan"].steps[0]
    amount = float(step.params.get("amount", 0.0))
    ticket = AuthTicket(
        id=f"TK{uuid.uuid4().hex[:8].upper()}",
        amount=amount,
        payee=step.params.get("payee", ""),
        payee_account=(bank.payee_info(step.params.get("payee", "")) or {}).get("account", ""),
        reason="；".join(session["risk"].reasons),
        created_at=datetime.now(),
        expires_at=datetime.now() + timedelta(seconds=300),
    )
    TICKETS[ticket.id] = ticket
    audit("auth", "agent", f"生成授权票据 {ticket.id}，等待子女远程授权",
          amount=amount, payee=ticket.payee, ttl_seconds=300)
    return ticket


def approve_ticket(req) -> dict:
    ticket = TICKETS.get(req.ticket_id)
    if ticket is None:
        return {"error": "票据不存在"}
    if ticket.status != "pending":
        return {"error": f"票据当前状态为 {ticket.status}，不可重复审批"}
    if datetime.now() > ticket.expires_at:
        ticket.status = "expired"
        return {"error": "票据已超时作废"}
    if not req.approve:
        ticket.status = "rejected"
        ticket.approver = req.approver
        audit("auth", req.approver, f"子女拒绝了票据 {ticket.id}", ticket=ticket.model_dump(mode="json"))
        return {"ticket": ticket.model_dump(mode="json"), "reason": "已拒绝"}
    # 子女可以下调金额与收紧有效期，但不能放宽到超出原始请求。
    if req.amount is not None and req.amount > ticket.amount:
        return {"error": "子女端不可上调金额，只能维持或下调"}
    if req.amount is not None:
        ticket.amount = req.amount
    if req.payee is not None and req.payee != ticket.payee:
        return {"error": "收款人不可变更，只能维持原收款人"}
    ticket.status = "approved"
    ticket.approver = req.approver
    ticket.expires_at = datetime.now() + timedelta(seconds=req.ttl_seconds)
    audit("auth", req.approver, f"子女授权通过 {ticket.id}（有效期 {req.ttl_seconds} 秒）",
          ticket=ticket.model_dump(mode="json"))
    return {"ticket": ticket.model_dump(mode="json"), "reason": "已授权"}


def expire_stale_tickets() -> None:
    now = datetime.now()
    for ticket in TICKETS.values():
        if ticket.status == "pending" and now > ticket.expires_at:
            ticket.status = "expired"


# --------------------------------------------------------------------------
# 账单分析 —— 升级为"月度资金安全体检报告"
# --------------------------------------------------------------------------


def _scoped_transactions(period: str) -> list[dict]:
    now = datetime.now()
    key = now.strftime("%Y-%m") if period == "month" else now.strftime("%Y")
    return [t for t in bank.TRANSACTIONS if t["date"].startswith(key)]


def bill_report(period: str = "month") -> dict:
    scoped = _scoped_transactions(period)
    label = "这个月" if period == "month" else "今年"
    out = [t for t in scoped if t["direction"] == "out"]
    income = sum(t["amount"] for t in scoped if t["direction"] == "in")
    total_out = sum(t["amount"] for t in out)
    by_category: dict[str, float] = {}
    for t in out:
        by_category[t["category"]] = by_category.get(t["category"], 0.0) + t["amount"]
    abnormal = detect_abnormal(period)
    ranked = sorted(by_category.items(), key=lambda kv: -kv[1])
    speech = (f"{label}一共支出 {money(total_out)} 元，收入 {money(income)} 元。"
              f"花得最多的是{'、'.join(f'{k} {money(v)} 元' for k, v in ranked[:3])}。")
    if abnormal:
        speech += f"另外我查出 {len(abnormal)} 笔可疑交易，您听听："
        speech += "；".join(f"{a['date']} {a['time']} 在{a['counterparty']}支出 {money(a['amount'])} 元，{a['reason']}"
                           for a in abnormal)
        speech += "。要不要我帮您冻结这类扣款？"
    else:
        speech += "没有查出可疑交易，账目是安全的。"
    return {"period": period, "label": label, "total_out": total_out, "income": income,
            "by_category": by_category, "abnormal": abnormal, "speech": speech}


def detect_abnormal(period: str = "month") -> list[dict]:
    """异常交易识别：直接接反诈规则库。深夜大额、连续小额试探、诱导型订阅。"""
    scoped = _scoped_transactions(period)
    abnormal: list[dict] = []
    for t in scoped:
        if t["direction"] != "out":
            continue
        hour = int(t["time"][:2])
        if not (7 <= hour < 22) and t["amount"] >= 1000:
            abnormal.append({**t, "reason": "深夜大额支出，与您平时的用卡习惯不符"})
    small = [t for t in scoped if t["direction"] == "out" and t["amount"] == 99.0]
    if len(small) >= 3:
        abnormal.append({**small[0], "amount": sum(t["amount"] for t in small),
                         "reason": f"{len(small)} 分钟内连续小额试探性扣款，是典型的养卡或试探手法"})
    for t in scoped:
        if t["category"] == "订阅" and t["counterparty"] == "养生大讲堂":
            abnormal.append({**t, "reason": "疑似诱导型订阅扣费，建议取消"})
    return abnormal


# --------------------------------------------------------------------------
# 扣费哨兵
# --------------------------------------------------------------------------


def scan_subscriptions() -> dict:
    active = [s for s in bank.SUBSCRIPTIONS if s["status"] == "active"]
    suspicious = [s for s in active if s["suspicious"]]
    monthly = sum(s["monthly_amount"] for s in active)
    if suspicious:
        s = suspicious[0]
        speech = (f"我查了一遍，您每月自动扣费一共 {money(monthly)} 元，"
                  f"其中「{s['merchant']}」每月扣 {money(s['monthly_amount'])} 元，"
                  f"{s['reason']}。下个月 {s['next_charge']} 又要扣了，要取消吗？")
    else:
        speech = f"您每月自动扣费一共 {money(monthly)} 元，没有查出可疑的诱导订阅。"
    return {"items": [dict(s) for s in bank.SUBSCRIPTIONS], "monthly_total": monthly,
            "suspicious_count": len(suspicious), "speech": speech}


# --------------------------------------------------------------------------
# 守护日历
# --------------------------------------------------------------------------


def due_guard_events(today: str | None = None) -> list[dict]:
    today_dt = datetime.strptime(today, "%Y-%m-%d") if today else datetime.now()
    due = []
    for event in bank.GUARD_EVENTS:
        try:
            event_dt = datetime.strptime(event["date"], "%Y-%m-%d")
        except (ValueError, TypeError):
            # 恶意或异常日期不能让整个守护日历接口崩掉，跳过它即可
            audit("risk", "rule-engine", f"守护日历事件「{event.get('title')}」日期非法，已跳过",
                  date=event.get("date"))
            continue
        trigger = event_dt - timedelta(days=event["advance_days"])
        if trigger.date() <= today_dt.date() <= event_dt.date():
            due.append({**event, "days_left": (event_dt.date() - today_dt.date()).days})
    return due


def run_guard_event(event_id: str, session: dict) -> dict:
    """跨场景联动动作链。每条动作链依然要过权限分级，联动不代表免确认。"""
    event = next(e for e in bank.GUARD_EVENTS if e["id"] == event_id)
    done: list[dict] = []
    acc = bank.ACCOUNTS["E001"]
    if event["lock_amount"]:
        acc["locked"] += event["lock_amount"]
        done.append({"step": "锁定活期", "detail": f"锁定 {money(event['lock_amount'])} 元作为「{event['title']}」专项预算",
                     "risk": "L0"})
    for action in event["actions"]:
        if action.startswith("锁定"):
            continue
        done.append({"step": action, "detail": f"已下单：{action}", "risk": "L1（复述确认后执行）"})
    return {"summary": f"「{event['title']}」联动动作链已执行",
            "event": event["title"], "steps": done}


# --------------------------------------------------------------------------
# 紧急止付
# --------------------------------------------------------------------------


def emergency_stop(reason: str) -> dict:
    frozen = []
    for acc in bank.ACCOUNTS.values():
        acc["emergency_stopped"] = True
        frozen.append(acc["id"])
    for card in bank.CARDS:
        card["status"] = "冻结"
    report = {
        "summary": "已冻结全部支付渠道（账户、卡片、快捷支付、代扣）",
        "frozen_accounts": frozen,
        "frozen_cards": [c["id"] for c in bank.CARDS],
        "cancelled_autopay": [s["merchant"] for s in bank.SUBSCRIPTIONS if s["status"] == "active"],
        "notified": ["张伟（儿子）", "张敏（女儿）", "人工坐席"],
        "reason": reason,
        "hotline": "96110",
    }
    audit("execute", "bank-core", "紧急止付已触发，全渠道冻结", **report)
    return report


# --------------------------------------------------------------------------
# 子女端：额度收紧 / 代购代缴
# --------------------------------------------------------------------------


def update_limits(free_limit: float | None = None, single_limit: float | None = None,
                  actor: str = "张伟") -> dict:
    """子女只能收紧额度，放宽必须走老人的临时提额审批流程。"""
    limits = bank.LIMITS["E001"]
    changes: list[str] = []
    if free_limit is not None:
        if free_limit > limits["free_limit"]:
            return {"error": f"子女端只能收紧免密额度，不能放宽（当前 {limits['free_limit']:.0f} 元）"}
        changes.append(f"免密额度 {limits['free_limit']:.0f} → {free_limit:.0f} 元")
        limits["free_limit"] = float(free_limit)
    if single_limit is not None:
        if single_limit > limits["single_limit"]:
            return {"error": f"子女端只能收紧单笔限额，不能放宽（当前 {limits['single_limit']:.0f} 元）"}
        changes.append(f"单笔限额 {limits['single_limit']:.0f} → {single_limit:.0f} 元")
        limits["single_limit"] = float(single_limit)
    limits["updated_by"] = actor
    audit("auth", actor, "子女端调整额度：" + ("；".join(changes) or "无变化"), **limits)
    return {"limits": limits, "changes": changes}


def proxy_action(kind: str, target: str, amount: float = 0.0, actor: str = "张伟") -> dict:
    """子女代购 / 代缴。不走转账权限分级，但依然要过理财适当性闸门并全程留痕。"""
    acc = bank.ACCOUNTS["E001"]
    if kind == "bill":
        acc["balance"] -= amount
        tx = {"id": bank.next_id("T", bank.TRANSACTIONS), "date": bank.today(),
              "time": datetime.now().strftime("%H:%M"), "amount": amount, "direction": "out",
              "counterparty": target, "category": "缴费", "channel": "子女代缴"}
        bank.TRANSACTIONS.insert(0, tx)
        audit("execute", actor, f"子女代缴 {target} {money(amount)} 元", tx=tx)
        return {"summary": f"已代缴「{target}」{money(amount)} 元", "tx": tx,
                "balance": acc["balance"]}
    product = next((p for p in bank.WEALTH_PRODUCTS if p["name"] == target), None)
    if product is None:
        return {"error": f"未找到产品 {target}"}
    gate = risk.wealth_gate(product)
    if gate.blocked:
        audit("risk", "rule-engine", f"子女代购「{target}」被适当性闸门拦截", reasons=gate.reasons)
        return {"error": f"「{target}」不在适老可售范围，已拦截",
                "risk": gate.model_dump(mode="json")}
    acc["balance"] -= amount
    bank.HOLDINGS.append({"id": bank.next_id("H", bank.HOLDINGS),
                          "product_id": product["id"], "amount": amount})
    audit("execute", actor, f"子女代购「{product['name']}」{money(amount)} 元",
          product=product["name"], amount=amount)
    return {"summary": f"已代购「{product['name']}」{money(amount)} 元", "balance": acc["balance"]}


# --------------------------------------------------------------------------
# 会话负载
# --------------------------------------------------------------------------


def _session_payload(session: dict) -> dict:
    decision: RiskDecision | None = session["risk"]
    plan: Plan = session["plan"]
    payload = {
        "session_id": session["id"],
        "status": session["status"],
        "intent": plan.intent.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "risk": decision.model_dump(mode="json") if decision else None,
        "elder_text": plan.readback,
        "ticket_id": session["ticket_id"],
        "result": session["result"],
    }
    if session["status"] == "executed" and session["result"]:
        # 执行完成后老人端应当看到结果，而不是继续显示操作前的复述文本
        summaries = [s.get("summary", "") for s in session["result"].get("steps", [])]
        notify_tools = [s.tool for s in plan.steps if s.tool in CHILD_NOTIFY_TOOLS]
        text = "。".join(s for s in summaries if s)
        if notify_tools:
            text += f"。这件事已经同步通知{bank.ACCOUNTS['C001']['owner']}了"
        if text:
            payload["elder_text"] = text
        return payload
    if decision is None:
        return payload
    if decision.blocked:
        payload["elder_text"] = (
            "这个操作我不能给您办，已经拦下来了，您的钱一分没动。" +
            "；".join(decision.reasons) + "。我已经通知您的儿子张伟。"
        )
        payload["action"] = "blocked"
    elif decision.level == RiskLevel.L2:
        payload["elder_text"] = (
            "这笔金额比较大，需要您儿子张伟在手机上点一下同意。" +
            "；".join(decision.reasons) + "。我已经把请求发给他了，您稍等一下。"
        )
        payload["action"] = "await_auth"
    elif decision.level == RiskLevel.L1:
        payload["action"] = "confirm_popup"
    else:
        payload["action"] = "confirm_voice"
    return payload


def reset_all() -> None:
    bank.reset()
    nlu.clear_llm_cache()
    SESSIONS.clear()
    TICKETS.clear()
    NOTIFICATIONS.clear()
    NOTIFIED_SESSIONS.clear()
    AUDIT.clear()
