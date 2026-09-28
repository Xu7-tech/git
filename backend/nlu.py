"""意图理解层。

两条路径：
1. 规则解析器（默认，永远可用，确定性，粤语/普通话双语词汇）。
2. LLM 适配器（配置 DASHSCOPE_API_KEY 后启用），只允许输出结构化意图。

无论走哪条路径，槽位都要与原文正则实体做一致性校验，不一致即中止执行 ——
这是防幻觉的第一道闸门。LLM 不持有资金接口调用权。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from . import bank
from .schemas import Intent

# --------------------------------------------------------------------------
# 中文口语金额
# --------------------------------------------------------------------------

CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}


def cn2num(text: str) -> float | None:
    """口语数字转数值。'五百'->500，'一千五'->1500，'两千'->2000，'十五'->15。"""
    section = 0.0
    number = 0.0
    last_unit = 0
    saw_zero = False
    for ch in text:
        if ch in CN_DIGITS:
            number = CN_DIGITS[ch]
            if ch in ("零", "〇"):
                saw_zero = True
        elif ch in CN_UNITS:
            unit = CN_UNITS[ch]
            if unit >= 10000:
                section = (section + number) * unit
                last_unit = unit
            else:
                section += (number or 1) * unit
                last_unit = unit
            number = 0.0
        else:
            return None
    # 口语省略末位单位：'一千五' => 1500，'一百五' => 150；
    # 出现"零"说明是完整读法（如'两百零一' = 201），不做省略推算。
    if number and last_unit >= 100 and number < 10 and not saw_zero:
        return section + number * (last_unit // 10)
    return section + number


MONEY_UNIT = r"(?:块钱|块|元|蚊|文|钱|錢)"
ARABIC_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(万|千|百)?\s*(块钱|块|元|蚊|文|钱|錢)?")
CN_NUM_RUN = re.compile(r"[零〇一二两三四五六七八九十百千万亿]{1,12}")
MONEY_VERB = "转|打|汇|买|充|交|存|取|缴|付|发|畀|过数|過數|转数|轉數"


def extract_amount(text: str) -> float | None:
    # 阿拉伯数字只有在跟了货币单位、或紧跟在转账动词后面时才算金额，
    # 否则"每个月1号"里的 1 会被误读成 1 元。
    for m in ARABIC_AMOUNT.finditer(text):
        unit, money_unit = m.group(2), m.group(3)
        if not unit and not money_unit:
            head = text[max(0, m.start() - 6):m.start()]
            if not re.search(f"(?:{MONEY_VERB})[^，。,.！!？?]{{0,6}}$", head):
                continue
        value = float(m.group(1))
        if unit == "万":
            value *= 10000
        elif unit == "千":
            value *= 1000
        elif unit == "百":
            value *= 100
        return value
    for run in CN_NUM_RUN.finditer(text):
        start, end = run.span()
        tail = text[end:end + 2]
        head = text[max(0, start - 6):start]
        if re.match(MONEY_UNIT, tail) or re.search(f"(?:{MONEY_VERB})[^，。,.！!？?]{{0,6}}$", head):
            value = cn2num(run.group())
            if value:
                return float(value)
    return None


# --------------------------------------------------------------------------
# 方言识别
# --------------------------------------------------------------------------

# 粤语特征词。繁简两种写法都要收，老人手写与语音转写出来的字形并不统一。
CANTONESE_MARKERS = (
    "畀", "俾", "唔", "係", "嘅", "啲", "哋", "冇", "喺", "佢", "咗", "乜", "啱",
    "睇", "嘢", "咁", "嚟", "攞", "摞", "閂", "闩", "冇得",
    "幾多", "几多", "幾時", "几时", "邊個", "边个", "點解", "点解", "邊度", "边度",
    "呢個", "呢个", "嗰個", "嗰个", "唔見", "唔见", "唔要", "唔好", "唔該", "唔该",
    "唔使", "唔得", "得唔得", "快啲", "張卡", "张卡", "銀紙", "银纸",
    "過數", "过数", "轉數", "转数", "過戶", "过户",
)

# 粤语 → 普通话 归一化表。
# 匹配时会把「原文」与「归一化结果」拼在一起做关键词匹配，原文里的粤语关键词依然有效，
# 所以这是加召回，不是替换。长词必须排在短词前（"唔見" 早于 "唔"），否则会被截成错词。
CANTONESE_NORMALIZE_RAW = (
    ("唔見", "丢失"), ("唔见", "丢失"), ("唔要", "不要"), ("唔好", "不要"),
    ("唔該", "麻烦"), ("唔该", "麻烦"), ("唔使", "不用"), ("唔得", "不行"),
    ("過數", "转账"), ("过数", "转账"), ("轉數", "转账"), ("转数", "转账"),
    ("過戶", "转账"), ("过户", "转账"),
    ("幾多", "多少"), ("几多", "多少"), ("幾時", "几时"),
    ("邊個", "哪个"), ("边个", "哪个"), ("邊度", "哪里"), ("边度", "哪里"),
    ("點解", "为什么"), ("点解", "为什么"),
    ("呢個", "这个"), ("呢个", "这个"), ("嗰個", "那个"), ("嗰个", "那个"),
    ("銀紙", "钱"), ("银纸", "钱"), ("張卡", "张卡"),
    ("畀", "给"), ("俾", "给"), ("係", "是"), ("嘅", "的"), ("啲", "些"),
    ("哋", "们"), ("佢", "他"), ("睇", "看"), ("喺", "在"), ("冇", "没有"),
    ("咗", "了"), ("啱", "对"), ("攞", "拿"), ("摞", "拿"),
    ("閂", "关闭"), ("闩", "关闭"), ("嘢", "事情"), ("嚟", "来"), ("咁", "这么"),
    ("唔", "不"),
)
CANTONESE_NORMALIZE = tuple(
    sorted(CANTONESE_NORMALIZE_RAW, key=lambda pair: -len(pair[0]))
)


def normalize_cantonese(text: str) -> str:
    """把粤语说法归一化成普通话，让同一套意图规则通吃两种方言。"""
    out = text
    for src, dst in CANTONESE_NORMALIZE:
        if src in out:
            out = out.replace(src, dst)
    return out


def detect_dialect(text: str) -> str:
    return "cantonese" if any(m in text for m in CANTONESE_MARKERS) else "mandarin"


def dialect_confidence(text: str) -> int:
    """命中的粤语特征词个数，用于演示时说明判断依据。"""
    return sum(1 for m in CANTONESE_MARKERS if m in text)


# --------------------------------------------------------------------------
# 收款人实体
# --------------------------------------------------------------------------

RELATION_NOTES = {
    "张伟": "大儿子，每月给您打赡养费",
    "张敏": "小女儿，在上海工作",
    "王小龙": "您的孙子，王小明的儿子",
    "王小虎": "邻居李阿姨的孙子，和您孙子同名",
    "王建国": "您的老伴",
    "李建国": "楼下棋友老李",
    "陈志强": "3 天前刚添加的好友，从未有过转账往来",
    "安全账户": "陌生收款人，已被多次举报",
}


def find_payees(text: str) -> tuple[list[str], list[str]]:
    """返回 (命中的别名, 命中的真实姓名)。手机号会先解析成对应收款人。"""
    aliases = [a for a in bank.ALIASES if a in text]
    names = [n for n in bank.PAYEES if n in text]
    for phone in PHONE_RE.findall(text):
        resolved = bank.find_payee_by_phone(phone)
        if resolved and resolved not in names:
            names.append(resolved)
    return aliases, names


PHONE_RE = re.compile(r"1[3-9]\d{9}")


SPLIT_HINT = re.compile(r"拼单|分摊|平分|凑钱|AA|aa|几个人分|个人分")
CARD_APPLY_HINT = re.compile(r"(申请|申請|办|辦|开|開)[^，。,.！!？?]{0,4}卡")
# 取消订阅允许中间夹字：'取消咗个订阅'、'退订个会员' 都要认出来
SUB_CANCEL_HINT = re.compile(
    r"(取消|退订|不要|cut|Cut|CUT)[^，。,.！!？?]{0,8}?"
    r"(订阅|訂閱|續費|续费|扣费|扣費|扣钱|扣錢|会员|會員|会籍|會籍)")
# 老人主动要求执行守护日历上的动作链
GUARD_RUN_HINT = re.compile(r"(帮我办|去办|办了吧|办一下|现在就办|现在就给|执行一下|走一遍)")
GUARD_TOPICS = (("生日", "生日"), ("复诊", "复诊"), ("体检", "复诊"),
                ("燃气", "燃气"), ("水电", "燃气"), ("缴费", "燃气"), ("物业", "燃气"))


def looks_like_split(text: str) -> bool:
    return bool(SPLIT_HINT.search(text)) and ("人" in text or "份" in text)


def looks_like_card_apply(text: str) -> bool:
    """'申请一张信用卡' / '办张卡' / '开卡' 这类说法，卡字前隔着量词也要认出来。"""
    return bool(CARD_APPLY_HINT.search(text))


def guard_run_target(text: str) -> str | None:
    """返回要执行的守护日历事件类型；None 表示这不是一个执行请求。

    注意与「最近有什么安排要提醒我」区分：只在出现明确的执行动词时才算执行请求。
    """
    if not GUARD_RUN_HINT.search(text):
        return None
    for key, target in GUARD_TOPICS:
        if key in text:
            return target
    return ""      # 说了要办但没指明哪一件 → 由调用方取最近一条


def schedule_hint(text: str) -> bool:
    """定期转账：'每个月/每月/定时/固定' 后紧跟一个转账动词。"""
    return bool(re.search(r"(每个月|每月|定时|固定)[^，。,.！!？?]{0,10}?(转|畀|给|缴)", text))


def parse_split(text: str) -> tuple[float | None, int | None]:
    """把'一共 600 块，4 个人分'解析成 (总金额, 人数)。"""
    people = None
    m_people = re.search(r"(\d+|[零〇一二两三四五六七八九十]{1,3})\s*(?:个)?人", text)
    if m_people:
        raw = m_people.group(1)
        people = int(raw) if raw.isdigit() else int(cn2num(raw) or 0)
    head = text[:m_people.start()] if m_people else text
    total = extract_amount(head) or extract_amount(text)
    return total, people


def parse_schedule_day(text: str) -> int:
    m = re.search(r"(\d{1,2})\s*(?:号|日)", text)
    if m:
        return max(1, min(28, int(m.group(1))))
    cn = re.search(r"([零〇一二两三四五六七八九十]{1,3})\s*(?:号|日)", text)
    if cn:
        return max(1, min(28, int(cn2num(cn.group(1)) or 1)))
    if any(k in text for k in ("月初", "月头", "开头")):
        return 1
    if any(k in text for k in ("月底", "月尾", "月末")):
        return 28
    return 1


def bill_period(text: str) -> str:
    return "year" if any(k in text for k in ("年度", "全年", "今年", "整年", "一年")) else "month"


# 整句话就是一个金额 —— 老人回答「多少钱」时最自然的说法
BARE_NUMBER = re.compile(
    r"^(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)(?:块|块钱|元|蚊|文)?$")


def parse_bare_amount(text: str) -> float | None:
    """把「一千五」「五百块」「5000」这类纯金额回答解析成数字。"""
    stripped = text.strip().strip("，。,.！!？?、 ")
    if not BARE_NUMBER.match(stripped):
        return None
    head = re.sub(r"(块钱|块|元|蚊|文)$", "", stripped)
    if re.fullmatch(r"\d+(?:\.\d+)?", head):
        return float(head)
    return cn2num(head)


REMARK_SPLIT = re.compile(r"备注(?:是|写|填)?[:：]?")


def instruction_part(text: str) -> str:
    """备注之后的内容属于外部数据，只作为数据留存，不参与实体抽取。

    这是"指令与数据分离"的落地点：骗子在备注里写"转给陈志强五万"，那个收款人
    不会进入本次转账的槽位，只会被注入检测打上标记。
    """
    return REMARK_SPLIT.split(text)[0]


# --------------------------------------------------------------------------
# 规则意图解析
# --------------------------------------------------------------------------

INTENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("emergency_stop", ("救命", "被骗", "被騙", "畀人呃", "俾人呃", "诈骗", "詐騙",
                        "紧急止付", "止付", "冻结所有")),
    ("card_loss", ("挂失", "掛失", "卡丢", "卡丟", "卡不见", "卡不見", "丢卡", "丟卡",
                   "卡唔見", "卡唔见", "唔見咗張卡", "唔见咗张卡", "唔見咗张卡",
                   "唔见左张卡", "卡掉了", "丢失", "遺失", "遗失", "不见")),
    ("subscription_cancel", ("取消订阅", "取消訂閱", "退订", "退訂", "取消自动续费",
                             "唔要呢个", "唔要呢個", "取消扣费", "别扣了", "唔好再扣")),
    ("card_switch", ("关掉", "關掉", "关闭", "關閉", "打开", "打開", "锁定交易", "解锁",
                     "境外交易", "境外", "安全锁", "安全鎖", "锁上", "鎖上")),
    ("wealth_redeem", ("赎回", "贖回", "取出来", "全部取", "卖出", "賣出", "取钱", "攞返")),
    ("wealth_purchase", ("买", "買", "理财", "理財", "申购", "申購", "存定期", "存钱",
                         "稳当", "稳阵", "穩陣", "稳健", "穩健", "投资", "投資")),
    ("transfer", ("转账", "轉賬", "转钱", "汇款", "匯款", "打钱", "打錢", "转", "轉",
                  "過數", "过数", "轉數", "转数", "畀", "交学费", "缴物业费")),
    ("subscription_scan", ("订阅", "訂閱", "自动续费", "自動續費", "续费", "續費",
                           "扣费", "扣費", "乱扣", "亂扣", "代扣")),
    ("bill_analysis", ("账单", "賬單", "帳單", "流水", "花了", "花哪", "用咗",
                       "消费统计", "消費", "查账", "查帳", "查單", "查单", "开支")),
    ("calendar_query", ("日历", "日曆", "提醒", "日程", "安排", "生日", "缴费日", "复诊",
                        "複診", "体检", "體檢", "守护日历", "守護日曆")),
    ("balance_query", ("余额", "餘額", "还有多少", "還有多少", "多少钱", "幾多錢", "几多钱",
                       "账户余额", "剩多少")),
]


def parse_intent_rule(text: str, dialect: str) -> Intent:
    instruction = instruction_part(text)
    normalized = normalize_cantonese(instruction)
    # 原文 + 归一化结果一起匹配：粤语关键词照样命中，普通话规则也能被归一化后的说法触发
    haystack = instruction if normalized == instruction else f"{instruction} {normalized}"
    aliases, names = find_payees(instruction)

    # 结构性判断优先于关键词匹配
    if looks_like_split(haystack):
        total, people = parse_split(instruction)
        share = round(total / people, 2) if total and people else None
        return Intent(name="bill_split",
                      slots={"alias_hits": aliases, "payee_hits": names,
                             "total": total, "people": people, "amount": share},
                      confidence=0.8, raw_utterance=text, dialect=dialect, source="rule")
    if schedule_hint(haystack):
        return Intent(name="transfer_schedule",
                      slots={"alias_hits": aliases, "payee_hits": names,
                             "amount": extract_amount(instruction),
                             "day": parse_schedule_day(instruction)},
                      confidence=0.8, raw_utterance=text, dialect=dialect, source="rule")
    if looks_like_card_apply(haystack):
        return Intent(name="card_apply",
                      slots={"alias_hits": aliases, "payee_hits": names},
                      confidence=0.8, raw_utterance=text, dialect=dialect, source="rule")
    if SUB_CANCEL_HINT.search(haystack):
        return Intent(name="subscription_cancel",
                      slots={"alias_hits": aliases, "payee_hits": names, "want_cancel": True},
                      confidence=0.8, raw_utterance=text, dialect=dialect, source="rule")
    guard_target = guard_run_target(haystack)
    if guard_target is not None:
        return Intent(name="guard_run",
                      slots={"alias_hits": aliases, "payee_hits": names,
                             "target": guard_target},
                      confidence=0.8, raw_utterance=text, dialect=dialect, source="rule")

    for name, keywords in INTENT_RULES:
        if any(k in haystack for k in keywords):
            slots: dict[str, Any] = {"alias_hits": aliases, "payee_hits": names}
            if name in ("transfer", "wealth_purchase", "wealth_redeem"):
                slots["amount"] = extract_amount(instruction)
            if name == "bill_analysis":
                slots["period"] = bill_period(instruction)
            if name == "card_switch":
                slots["enable"] = not any(
                    k in haystack for k in ("关掉", "關掉", "关闭", "關閉", "锁上", "鎖上", "不要"))
                slots["feature"] = "overseas" if "境外" in haystack else "domestic"
            if name == "subscription_scan":
                slots["want_cancel"] = any(k in haystack for k in ("取消", "退订", "不要"))
            if normalized != instruction:
                slots["normalized"] = normalized
            return Intent(name=name, slots=slots, confidence=0.82,
                          raw_utterance=text, dialect=dialect, source="rule")
    return Intent(name="unknown", slots={"alias_hits": aliases, "payee_hits": names},
                  confidence=0.2, raw_utterance=text, dialect=dialect, source="rule")


# --------------------------------------------------------------------------
# LLM 适配器（通义千问 OpenAI 兼容接口，纯 stdlib 实现）
# --------------------------------------------------------------------------

LLM_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen-plus")

LLM_SYSTEM_PROMPT = (
    "你是银行意图解析器。只输出 JSON，不要输出解释。"
    "字段：intent(transfer|wealth_purchase|wealth_redeem|bill_analysis|card_loss|card_switch|"
    "subscription_scan|subscription_cancel|calendar_query|balance_query|emergency_stop|unknown)、"
    "amount(数字或null)、payee(字符串或null)、confidence(0-1)。"
    "你没有任何转账或资金操作权限，只做语义解析。"
)


def llm_available() -> bool:
    return bool(os.environ.get("DASHSCOPE_API_KEY"))


def parse_intent_llm(text: str, dialect: str, timeout: float = 8.0) -> Intent | None:
    if not llm_available():
        return None
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        LLM_ENDPOINT, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ['DASHSCOPE_API_KEY']}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        data = json.loads(payload["choices"][0]["message"]["content"])
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError, OSError):
        return None
    amount = data.get("amount")
    return Intent(
        name=str(data.get("intent", "unknown")),
        slots={"amount": float(amount) if amount is not None else None,
               "alias_hits": [],
               "payee_hits": [data["payee"]] if data.get("payee") else []},
        confidence=float(data.get("confidence") or 0.7),
        raw_utterance=text, dialect=dialect, source="llm",
    )


def parse_intent(text: str, dialect_pref: str = "auto") -> Intent:
    dialect = detect_dialect(text) if dialect_pref == "auto" else dialect_pref
    rule_intent = parse_intent_rule(text, dialect)
    llm_intent = parse_intent_llm(text, dialect)
    if llm_intent is None:
        return rule_intent
    ok, note = verify_consistency(llm_intent.slots, text)
    if not ok:
        # LLM 与原文实体不一致 —— 不采信 LLM，回落到确定性的规则解析。
        rule_intent.slots["consistency_note"] = f"LLM 槽位与原文不一致（{note}），已回落规则解析"
        return rule_intent
    if llm_intent.name == "unknown":
        return rule_intent
    return llm_intent


# --------------------------------------------------------------------------
# 一致性校验（防幻觉 / 防注入改参）
# --------------------------------------------------------------------------


def raw_entities(text: str) -> dict[str, Any]:
    instruction = instruction_part(text)
    aliases, names = find_payees(instruction)
    return {
        "amount": extract_amount(instruction),
        "payees": [*names, *[n for a in aliases for n in bank.ALIASES[a]]],
        "alias_hits": aliases,
        "payee_hits": names,
    }


def verify_consistency(slots: dict[str, Any], raw_text: str) -> tuple[bool, str]:
    """执行参数必须来自用户原话。任何对不上原文的参数都视为模型幻觉或注入改参。"""
    entities = raw_entities(raw_text)
    amount = slots.get("amount")
    if amount is not None and entities["amount"] is None:
        return False, f"模型给出金额 {amount}，但原话中没有任何金额"
    if amount is not None and entities["amount"] is not None \
            and abs(float(amount) - entities["amount"]) > 0.001:
        # 拼单分摊这类场景的金额是按总额与人数算出来的派生值，
        # 只要等于"总额 ÷ 人数"就视为与原文一致，不算幻觉。
        total, people = parse_split(raw_text)
        derived = bool(total and people) and abs(float(amount) - round(total / people, 2)) < 0.001
        if not derived:
            return False, f"模型金额 {amount} 与原话金额 {entities['amount']} 不符"
    payee = slots.get("payee") or (slots.get("payee_hits") or [None])[0]
    if payee and entities["payees"] and payee not in entities["payees"]:
        return False, f"模型收款人 {payee} 未在原话中出现"
    return True, ""
