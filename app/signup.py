"""自助开户 / 访客留资的可测试纯逻辑。

- 行业目录：申请表下拉选项、自由文本 → 行业部门 key 的模糊匹配；
- 开户时把所选行业写进租户（tenants.industries_json + tenant_industry 双写）；
- 老板首次自选行业：仅当租户当前一个行业都没有时允许，防止绕过套餐加行业；
- 内存态限流工具：按天计数、当天名额原子占用/回滚；
- 字段截断与 root 充值点数校验。

本模块不依赖 fastapi，main.py 只做路由层调用。
"""
import json
import math
import re
import threading
import time

from . import db

# 行业 key → (下拉里给老板看的中文名, 自由文本别名)。别名越长越具体，
# 匹配时优先取最长命中，比如「汽车美容」归汽车而不是美容。
INDUSTRY_CATALOG = {
    "restaurant": ("餐饮", (
        "餐饮", "餐厅", "餐馆", "饭店", "饭馆", "酒楼", "食堂", "火锅", "烧烤",
        "烤肉", "快餐", "小吃", "面馆", "粉店", "米粉", "米线", "麻辣烫", "串串",
        "中餐", "西餐", "日料", "韩料", "料理", "饺子", "包子", "炸鸡", "汉堡",
        "披萨", "川菜", "湘菜", "粤菜", "外卖", "私房菜", "大排档", "茶餐厅",
    )),
    "tea_coffee": ("茶饮咖啡", (
        "茶饮", "奶茶", "咖啡", "茶咖", "果茶", "柠檬茶", "饮品", "现制饮",
        "茶饮咖啡", "茶咖现制", "新茶饮",
    )),
    "beauty": ("美容美业", (
        "美容", "美业", "美发", "美甲", "美睫", "理发", "发廊", "发型", "造型",
        "医美", "皮肤管理", "spa", "养发", "头疗", "纹绣", "化妆", "美容院",
        "美容美业", "美容美发",
    )),
    "fitness": ("健身瑜伽", (
        "健身", "瑜伽", "普拉提", "私教", "健身房", "拳馆", "搏击", "健身瑜伽",
    )),
    "auto": ("汽车服务", (
        "汽车", "汽修", "修车", "洗车", "车行", "4s", "轮胎", "汽配", "贴膜",
        "钣金", "喷漆", "车辆保养", "汽车美容", "汽车服务", "汽车后市场",
    )),
    "hotel": ("酒店住宿", (
        "酒店", "宾馆", "民宿", "旅馆", "客栈", "旅店", "住宿", "酒店住宿",
    )),
    "pet": ("宠物", (
        "宠物", "萌宠", "宠店", "猫舍", "犬舍", "狗狗", "猫咪", "宠物医院",
    )),
    "pharmacy": ("零售药房", (
        "药房", "药店", "大药房", "医药", "药品", "药铺", "零售药房",
    )),
    "convenience": ("便利店", (
        "便利店", "便利", "小卖部", "士多", "烟酒店", "社区店",
    )),
    "grocery": ("商超生鲜", (
        "超市", "商超", "生鲜", "菜市", "果蔬", "水果店", "卖场", "粮油",
        "商超生鲜",
    )),
    "snack": ("量贩零食", (
        "零食", "量贩", "炒货", "坚果", "糖果", "休闲食品", "量贩零食",
    )),
}

OTHER_KEY = "other"
OTHER_LABEL = "其他"
INDUSTRY_TEXT_MAX = 30
APPLY_INDUSTRY_PREFIX = "行业："


def _norm(text) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def industry_label(key: str, fallback: str = "") -> str:
    entry = INDUSTRY_CATALOG.get(str(key or ""))
    return entry[0] if entry else str(fallback or key or "")


def industry_options(depts) -> list:
    """公开下拉选项：只含当前真实存在的行业部门，只回 key + 中文名。

    ``depts`` 是 departments.list_depts() 的结果（至少含 key/name）。目录里
    有中文短名的按目录顺序排前，新入驻但目录还没收录的部门排在后面。
    """
    present = {}
    for dept in depts or []:
        key = str((dept or {}).get("key") or "").strip()
        if key and key not in present:
            present[key] = str(dept.get("name") or key)
    out = [
        {"key": key, "name": INDUSTRY_CATALOG[key][0]}
        for key in INDUSTRY_CATALOG if key in present
    ]
    out.extend(
        {"key": key, "name": name}
        for key, name in present.items() if key not in INDUSTRY_CATALOG
    )
    return out


def resolve_industry(text, valid_keys=None, dept_names=None):
    """把老板填的行业文本匹配成一个行业 key；拿不准返回 None。

    1. 完全等于 key / 中文名 / 部门原名 → 直接命中；
    2. 否则按别名子串匹配，取「最长命中别名」最长的行业；
    3. 两个行业打平（如「奶茶火锅店」）视为拿不准，返回 None，
       交给老板登录后自己选，绝不瞎猜多绑。
    ``valid_keys`` 限定只在现存部门里选；``dept_names`` 是 {key: 部门原名}。
    """
    raw = _norm(text)
    if not raw or raw in (_norm(OTHER_LABEL), OTHER_KEY):
        return None
    allowed = set(valid_keys) if valid_keys is not None else set(INDUSTRY_CATALOG)
    for key in allowed:
        names = {_norm(key), _norm(industry_label(key))}
        if dept_names and dept_names.get(key):
            names.add(_norm(dept_names[key]))
        if raw in names:
            return key
    best_len, best_keys = 0, []
    for key, (label, aliases) in INDUSTRY_CATALOG.items():
        if key not in allowed:
            continue
        hit = max(
            (len(_norm(alias)) for alias in (label, *aliases)
             if _norm(alias) and _norm(alias) in raw),
            default=0,
        )
        if hit > best_len:
            best_len, best_keys = hit, [key]
        elif hit and hit == best_len:
            best_keys.append(key)
    return best_keys[0] if len(best_keys) == 1 else None


def normalize_apply_industry(industry, industry_text, valid_keys) -> str:
    """申请表提交的行业 → 写进申请备注的一行人话（空串表示没填）。

    选了下拉里的真实行业写中文名；选「其他」或旧客户端只填自由文本时，
    写截断后的原文，开户时再模糊匹配。
    """
    key = str(industry or "").strip()
    if key in set(valid_keys or ()):
        return industry_label(key)
    return clip(industry_text, INDUSTRY_TEXT_MAX)


def compose_apply_note(industry_line: str, note: str, note_max: int = 200) -> str:
    note = clip(note, note_max)
    if not industry_line:
        return note
    head = APPLY_INDUSTRY_PREFIX + clip(industry_line, INDUSTRY_TEXT_MAX)
    return head + ("\n" + note if note else "")


def industry_key_for_apply(apply_row: dict, valid_keys=None, dept_names=None):
    """开户时从申请单推断行业：先看备注里的「行业：」行，再退回企业名兜底。"""
    note = str((apply_row or {}).get("note") or "")
    for line in note.splitlines():
        line = line.strip()
        for prefix in (APPLY_INDUSTRY_PREFIX, "行业:"):
            if line.startswith(prefix):
                key = resolve_industry(line[len(prefix):], valid_keys, dept_names)
                if key:
                    return key
    return resolve_industry(
        (apply_row or {}).get("company") or "", valid_keys, dept_names
    )


def write_tenant_industries(connection, tid: int, keys, now=None):
    """在调用方事务里整体替换租户行业：旧字段与规范化映射双写。"""
    now = time.time() if now is None else now
    keys = list(dict.fromkeys(str(k) for k in (keys or []) if k))
    connection.execute(
        "UPDATE tenants SET industries_json=?,updated_at=? WHERE id=?",
        (json.dumps(keys, ensure_ascii=False), now, tid),
    )
    connection.execute("DELETE FROM tenant_industry WHERE tenant_id=?", (tid,))
    for position, key in enumerate(keys):
        connection.execute(
            "INSERT INTO tenant_industry(tenant_id,industry_key,is_primary,created_at) "
            "VALUES(?,?,?,?)",
            (tid, key, 1 if position == 0 else 0, now),
        )


class IndustryChoiceError(ValueError):
    """老板自选行业被拒；``status`` 给路由层映射 HTTP 状态码。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def claim_first_industry(tid: int, key: str, valid_keys) -> str:
    """老板首次自选 1 个行业；租户已有任何行业时拒绝（加行业走套餐/客服）。"""
    tid = int(tid)
    key = str(key or "").strip()
    if tid == 1:
        raise IndustryChoiceError(400, "平台总部不需要选择行业")
    if key not in set(valid_keys or ()):
        raise IndustryChoiceError(400, "请从列表里选一个行业")
    with db.atomic() as connection:
        tenant = connection.execute(
            "SELECT id FROM tenants WHERE id=? AND enabled=1", (tid,)
        ).fetchone()
        if not tenant:
            raise IndustryChoiceError(404, "企业不存在或已停用")
        if connection.execute(
            "SELECT 1 FROM tenant_industry WHERE tenant_id=? LIMIT 1", (tid,)
        ).fetchone():
            raise IndustryChoiceError(
                409, "您的企业已经开通了行业；想再加行业请到套餐页或联系顾问"
            )
        write_tenant_industries(connection, tid, [key])
    return key


def tenant_needs_industry(tid: int) -> bool:
    if int(tid or 0) <= 1:
        return False
    return not db.one(
        "SELECT 1 ok FROM tenant_industry WHERE tenant_id=? LIMIT 1", (int(tid),)
    )


# ---------------- 字段清洗 ----------------
def clip(value, limit: int) -> str:
    """去首尾空白、去控制字符后截断；邮件与入库都用截断后的值。"""
    text = str(value or "").strip()
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()
    return text[: max(0, int(limit))]


GRANT_POINTS_MAX = 1_000_000


def parse_grant_points(value) -> float:
    """root 充值点数：必须是大于 0 的有限数（扣点不走这个入口）。"""
    if isinstance(value, bool):
        raise ValueError("充值点数必须是数字")
    try:
        points = float(value)
    except (TypeError, ValueError):
        raise ValueError("充值点数必须是数字") from None
    if not math.isfinite(points) or points <= 0:
        raise ValueError("充值点数必须大于 0")
    if points > GRANT_POINTS_MAX:
        raise ValueError(f"单次充值不能超过 {GRANT_POINTS_MAX} 点")
    return points


# ---------------- 内存态限流 ----------------
def _today(now=None) -> int:
    return int((time.time() if now is None else now) // 86400)


class DailyCounter:
    """按键、按天计数（跨天自动清零），缓存有界；线程安全。"""

    def __init__(self, limit: int, max_keys: int = 5000):
        self.limit = int(limit)
        self.max_keys = int(max_keys)
        self._hits: dict = {}
        self._lock = threading.Lock()

    def hit(self, key: str, now=None) -> bool:
        """记一次，返回这次是否已超当天上限。"""
        today = _today(now)
        with self._lock:
            count, day = self._hits.get(key, (0, today))
            count = count + 1 if day == today else 1
            self._hits[key] = (count, today)
            if len(self._hits) > self.max_keys:
                for stale in [k for k, (_, d) in self._hits.items() if d != today]:
                    self._hits.pop(stale, None)
                while len(self._hits) > self.max_keys:
                    self._hits.pop(next(iter(self._hits)))
            return count > self.limit

    def clear(self):
        with self._lock:
            self._hits.clear()

    def __len__(self):
        return len(self._hits)


class DailyQuota:
    """当天全站名额：检查与占用一步完成，失败可回滚，避免并发超发。"""

    def __init__(self):
        self._used = 0
        self._day = 0
        self._lock = threading.Lock()

    def try_reserve(self, cap: int, now=None) -> bool:
        today = _today(now)
        with self._lock:
            if self._day != today:
                self._used, self._day = 0, today
            if self._used >= int(cap):
                return False
            self._used += 1
            return True

    def release(self, now=None):
        """占用后开户失败时退回名额（跨天了就不用退）。"""
        today = _today(now)
        with self._lock:
            if self._day == today and self._used > 0:
                self._used -= 1

    def used(self, now=None) -> int:
        with self._lock:
            return self._used if self._day == _today(now) else 0

    def reset(self):
        with self._lock:
            self._used, self._day = 0, 0
