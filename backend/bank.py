"""模拟银行核心：账户、通讯录别名簿、收款人名录、流水、理财、卡片、订阅、限额。"""
from __future__ import annotations

import copy
from datetime import datetime

# --------------------------------------------------------------------------
# 账户
# --------------------------------------------------------------------------

ACCOUNTS: dict[str, dict] = {
    "E001": {
        "id": "E001",
        "owner": "张桂芬",
        "role": "elder",
        "balance": 28650.00,
        "locked": 0.0,
        "emergency_stopped": False,
    },
    "C001": {
        "id": "C001",
        "owner": "张伟",
        "role": "child",
        "balance": 120000.00,
        "locked": 0.0,
        "emergency_stopped": False,
    },
}

# 口语别名簿：老人只说"孙子"，系统负责翻译成人名，并暴露重名歧义。
ALIASES: dict[str, list[str]] = {
    "儿子": ["张伟"],
    "女儿": ["张敏"],
    "孙子": ["王小龙", "王小虎"],  # 重名：一个是自家孙子，一个是邻居家孙子
    "老伴": ["王建国"],
    "老李": ["李建国"],
    # 粤语口语说法
    "个仔": ["张伟"],
    "个女": ["张敏"],
    "个孙": ["王小龙", "王小虎"],
    # 繁体写法（老人手写或粤语转写常见）
    "個仔": ["张伟"],
    "個女": ["张敏"],
    "個孫": ["王小龙", "王小虎"],
    "兒子": ["张伟"],
    "女兒": ["张敏"],
    "孫子": ["王小龙", "王小虎"],
}

# 收款人名录。known_days / complaints / whitelisted 直接喂给规则引擎。
PAYEES: dict[str, dict] = {
    "张伟": {"account": "6222 0100 1001", "phone": "13800001111", "bank": "工商银行", "city": "上海",
             "known_days": 3650, "complaints": 0, "whitelisted": True},
    "张敏": {"account": "6222 0100 1002", "phone": "13800002222", "bank": "工商银行", "city": "上海",
             "known_days": 3650, "complaints": 0, "whitelisted": True},
    "王小龙": {"account": "6228 4501 2001", "phone": "13800003333", "bank": "建设银行", "city": "上海",
               "known_days": 3000, "complaints": 0, "whitelisted": True},
    "王小虎": {"account": "6228 4501 2002", "phone": "13800004444", "bank": "建设银行", "city": "上海",
               "known_days": 1800, "complaints": 0, "whitelisted": False},
    "王建国": {"account": "6222 0100 1003", "phone": "13800005555", "bank": "工商银行", "city": "上海",
               "known_days": 14000, "complaints": 0, "whitelisted": True},
    "李建国": {"account": "6228 4501 2003", "phone": "13800006666", "bank": "农业银行", "city": "上海",
               "known_days": 2200, "complaints": 0, "whitelisted": True},
    "陈志强": {"account": "6217 0033 9001", "phone": "13900009999", "bank": "某地方银行", "city": "境外",
               "known_days": 3, "complaints": 7, "whitelisted": False},
    "安全账户": {"account": "6217 0033 0000", "bank": "未知", "city": "境外",
                 "known_days": 0, "complaints": 12, "whitelisted": False},
    "监管账户": {"account": "6217 0033 0100", "bank": "未知", "city": "境外",
                 "known_days": 0, "complaints": 9, "whitelisted": False},
    # 无话术、纯行为异常剧本用的收款人：认识不久、未被举报、同城
    "赵师傅": {"account": "6228 4501 2009", "bank": "邮储银行", "city": "上海",
               "known_days": 5, "complaints": 0, "whitelisted": False},
    # 常用但异地的收款人，用于单独验证"异地"这一条升权规则
    "陈美玲": {"account": "6228 4501 2010", "bank": "招商银行", "city": "广州",
               "known_days": 1200, "complaints": 0, "whitelisted": True},
}

LOCAL_CITY = "上海"

# --------------------------------------------------------------------------
# 流水（含为异常识别准备的样本）
# --------------------------------------------------------------------------

TRANSACTIONS: list[dict] = [
    {"id": "T0001", "date": "2026-09-28", "time": "02:13", "amount": 3999.00,
     "direction": "out", "counterparty": "某网络科技", "category": "其他", "channel": "快捷支付"},
    {"id": "T0002", "date": "2026-09-26", "time": "21:40", "amount": 99.00,
     "direction": "out", "counterparty": "幸运抽奖平台", "category": "其他", "channel": "快捷支付"},
    {"id": "T0003", "date": "2026-09-26", "time": "21:42", "amount": 99.00,
     "direction": "out", "counterparty": "幸运抽奖平台", "category": "其他", "channel": "快捷支付"},
    {"id": "T0004", "date": "2026-09-26", "time": "21:44", "amount": 99.00,
     "direction": "out", "counterparty": "幸运抽奖平台", "category": "其他", "channel": "快捷支付"},
    {"id": "T0005", "date": "2026-09-26", "time": "21:46", "amount": 99.00,
     "direction": "out", "counterparty": "幸运抽奖平台", "category": "其他", "channel": "快捷支付"},
    {"id": "T0006", "date": "2026-09-20", "time": "10:05", "amount": 168.00,
     "direction": "out", "counterparty": "燃气公司", "category": "缴费", "channel": "代扣"},
    {"id": "T0007", "date": "2026-09-15", "time": "09:30", "amount": 199.00,
     "direction": "out", "counterparty": "养生大讲堂", "category": "订阅", "channel": "代扣"},
    {"id": "T0008", "date": "2026-09-12", "time": "08:20", "amount": 46.50,
     "direction": "out", "counterparty": "永和菜市场", "category": "餐饮", "channel": "扫码"},
    {"id": "T0009", "date": "2026-09-11", "time": "15:02", "amount": 128.00,
     "direction": "out", "counterparty": "华氏大药房", "category": "医疗", "channel": "刷卡"},
    {"id": "T0010", "date": "2026-09-01", "time": "09:00", "amount": 25.00,
     "direction": "out", "counterparty": "视频会员", "category": "订阅", "channel": "代扣"},
    {"id": "T0011", "date": "2026-09-05", "time": "11:00", "amount": 3000.00,
     "direction": "in", "counterparty": "张伟", "category": "赡养费", "channel": "转账"},
    {"id": "T0012", "date": "2026-09-08", "time": "16:40", "amount": 226.00,
     "direction": "out", "counterparty": "联华超市", "category": "日用", "channel": "扫码"},
    # 早前月份的常规流水，用于年度账单对比（均为白天小额，不触发异常识别）
    {"id": "T0013", "date": "2026-08-22", "time": "10:12", "amount": 312.50,
     "direction": "out", "counterparty": "永和菜市场", "category": "餐饮", "channel": "扫码"},
    {"id": "T0014", "date": "2026-08-14", "time": "14:30", "amount": 96.00,
     "direction": "out", "counterparty": "华氏大药房", "category": "医疗", "channel": "刷卡"},
    {"id": "T0015", "date": "2026-08-05", "time": "09:00", "amount": 3000.00,
     "direction": "in", "counterparty": "张伟", "category": "赡养费", "channel": "转账"},
    {"id": "T0016", "date": "2026-07-19", "time": "11:20", "amount": 268.00,
     "direction": "out", "counterparty": "联华超市", "category": "日用", "channel": "扫码"},
    {"id": "T0017", "date": "2026-07-06", "time": "09:00", "amount": 3000.00,
     "direction": "in", "counterparty": "张伟", "category": "赡养费", "channel": "转账"},
]

# 老人自己设置的定期转账（生活费、物业费一类）
SCHEDULES: list[dict] = []

# --------------------------------------------------------------------------
# 理财（适老视角：只放 R1/R2 给老人自主购买）
# --------------------------------------------------------------------------

WEALTH_PRODUCTS: list[dict] = [
    {"id": "W01", "name": "安心存三个月", "risk": "R1", "annual": 1.85, "min": 1000, "elder_ok": True},
    {"id": "W02", "name": "国债三年期", "risk": "R1", "annual": 2.38, "min": 100, "elder_ok": True},
    {"id": "W03", "name": "货币基金A", "risk": "R1", "annual": 1.62, "min": 1, "elder_ok": True},
    {"id": "W04", "name": "稳健债基", "risk": "R2", "annual": 3.10, "min": 1000, "elder_ok": True},
    {"id": "W05", "name": "均衡混合基金", "risk": "R3", "annual": 6.50, "min": 1000, "elder_ok": False},
    {"id": "W06", "name": "成长股票基金", "risk": "R4", "annual": 11.00, "min": 1000, "elder_ok": False},
    {"id": "W07", "name": "高收益养老理财", "risk": "R5", "annual": 18.00, "min": 5000,
     "elder_ok": False, "suspicious": True, "reason": "非持牌机构产品，命中投资诱导话术库"},
]

HOLDINGS: list[dict] = [
    {"id": "H01", "product_id": "W03", "amount": 5000.00},
]

# --------------------------------------------------------------------------
# 卡片与限额
# --------------------------------------------------------------------------

CARDS: list[dict] = [
    {"id": "CARD-001", "type": "借记卡", "tail": "6688", "status": "正常",
     "loss_reported": False, "overseas_enabled": True, "replacement": None},
]

# 新卡申请工单（卡申请场景）
CARD_APPLICATIONS: list[dict] = []

LIMITS: dict[str, dict] = {
    "E001": {
        "free_limit": 200.0,
        "single_limit": 5000.0,
        "daily_limit": 50000.0,
        "overseas_enabled": True,
        "updated_by": "system",
    }
}

# --------------------------------------------------------------------------
# 订阅 / 代扣
# --------------------------------------------------------------------------

SUBSCRIPTIONS: list[dict] = [
    {"id": "SUB-001", "merchant": "养生大讲堂", "category": "保健品/养生课",
     "monthly_amount": 199.00, "next_charge": "2026-10-15", "suspicious": True,
     "reason": "诱导型订阅：养生课高频加价，连续 3 个月扣费", "status": "active"},
    {"id": "SUB-002", "merchant": "视频会员", "category": "影音娱乐",
     "monthly_amount": 25.00, "next_charge": "2026-10-01", "suspicious": False,
     "reason": "", "status": "active"},
    {"id": "SUB-003", "merchant": "社区团购", "category": "生活服务",
     "monthly_amount": 0.00, "next_charge": "-", "suspicious": False,
     "reason": "免密小额，无固定扣费", "status": "active"},
]

# --------------------------------------------------------------------------
# 守护日历（跨场景联动）
# --------------------------------------------------------------------------

GUARD_EVENTS: list[dict] = [
    {
        "id": "G-生日", "title": "老伴王建国生日", "date": "2026-10-16",
        "advance_days": 2, "kind": "birthday", "deployed_by": "张伟",
        "lock_amount": 1000.0, "actions": ["锁定活期 1000 元", "生日前 2 天订购鲜花", "生日前 2 天订购蛋糕"],
        "steps": [
            {"tool": "wealth.lock_liquid", "summary": "锁定活期 1000 元作为生日预算"},
        ],
    },
    {
        "id": "G-燃气费", "title": "燃气费缴纳", "date": "2026-10-20",
        "advance_days": 1, "kind": "utility", "deployed_by": "张伟",
        "lock_amount": 0.0, "actions": ["到期提醒", "待确认代缴"],
        "steps": [
            {"tool": "bill.pay", "summary": "燃气费 168 元待确认缴纳"},
        ],
    },
    {
        "id": "G-复诊", "title": "社区医院复诊", "date": "2026-10-08",
        "advance_days": 2, "kind": "medical", "deployed_by": "张敏",
        "lock_amount": 0.0, "actions": ["复诊提醒", "叫车提醒"],
        "steps": [
            {"tool": "notify.reminder", "summary": "复诊提醒 + 叫车提醒"},
        ],
    },
]


def reset() -> None:
    """把可变状态恢复到初始值（演示重置用）。"""
    global TRANSACTIONS, SUBSCRIPTIONS, CARDS, HOLDINGS, LIMITS, SCHEDULES
    global CARD_APPLICATIONS, GUARD_EVENTS
    TRANSACTIONS = copy.deepcopy(_SEED["transactions"])
    SUBSCRIPTIONS = copy.deepcopy(_SEED["subscriptions"])
    CARDS = copy.deepcopy(_SEED["cards"])
    HOLDINGS = copy.deepcopy(_SEED["holdings"])
    LIMITS = copy.deepcopy(_SEED["limits"])
    SCHEDULES = []
    CARD_APPLICATIONS = []
    GUARD_EVENTS = copy.deepcopy(_SEED["guard_events"])
    for acc_id, acc in ACCOUNTS.items():
        acc["balance"] = _SEED["balances"][acc_id]
        acc["locked"] = 0.0
        acc["emergency_stopped"] = False


def payee_info(name: str) -> dict | None:
    return PAYEES.get(name)


def find_payee_by_phone(phone: str) -> str | None:
    """按手机号找收款人 —— 对应赛题「按手机号转账」。"""
    digits = "".join(ch for ch in phone if ch.isdigit())
    for name, info in PAYEES.items():
        if info.get("phone") == digits:
            return name
    return None


def resolve_alias(alias: str) -> list[str]:
    return ALIASES.get(alias, [])


def next_id(prefix: str, seq: list) -> str:
    return f"{prefix}{len(seq) + 1:04d}"


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


_SEED = {
    "transactions": copy.deepcopy(TRANSACTIONS),
    "subscriptions": copy.deepcopy(SUBSCRIPTIONS),
    "cards": copy.deepcopy(CARDS),
    "holdings": copy.deepcopy(HOLDINGS),
    "limits": copy.deepcopy(LIMITS),
    "guard_events": copy.deepcopy(GUARD_EVENTS),
    "balances": {k: v["balance"] for k, v in ACCOUNTS.items()},
}
