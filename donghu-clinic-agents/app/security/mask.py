# -*- coding: utf-8 -*-
"""敏感信息脱敏模块：所有对外输出的业务数据必须先经此模块脱敏，防止泄露红线被突破。"""  # 模块职责说明
from app.config import get_settings                      # 引入统一配置（脱敏规则来自 config.yaml）


def mask_name(name: str) -> str:                         # 姓名脱敏
    """姓名脱敏：保留姓氏，其余以 * 代替。

    入参：name 原始姓名字符串。出参：脱敏后姓名（如 张*、欧*阳）。
    失败行为：空值原样返回空字符串；单字符返回 *。
    """
    if not name:                                         # 空值防御：避免 None/空串越界
        return ""                                        # 空值输出空串
    if len(name) == 1:                                   # 单字符姓名无中间部分可打码
        return "*"                                       # 输出单个星号，杜绝全名输出
    return name[0] + "*" * (len(name) - 1)               # 保留首字符，其余打码（保持长度，便于展示）

def mask_phone(phone: str) -> str:                       # 手机号脱敏
    """手机号脱敏：保留前 3 位与后 4 位，中间 4 位打码。

    入参：phone 原始手机号。出参：形如 138****5678 的字符串。
    失败行为：非 11 位号码整体打码为 ******，避免部分泄露。
    """
    if not phone:                                        # 空值防御
        return ""                                        # 空进空出
    digits = "".join(ch for ch in str(phone) if ch.isdigit())  # 仅保留数字字符
    if len(digits) != 11:                                # 非标准手机号一律全打码——宁可不展示也不能泄露
        return "*" * max(len(digits), 6)                 # 全打码输出
    return digits[:3] + "****" + digits[-4:]             # 标准 11 位：前3+****+后4

def mask_id_card(id_card: str) -> str:                   # 身份证脱敏
    """身份证脱敏：仅保留前 4 位与后 2 位，其余打码。

    入参：id_card 原始证件号。出参：形如 1101**********12 的字符串。
    失败行为：长度不足 8 位时全打码，防止通过过短片段反推。
    """
    if not id_card:                                      # 空值防御
        return ""                                        # 空进空出
    s = str(id_card)                                     # 统一为字符串
    if len(s) < 8:                                       # 过短证件号无法安全保留片段
        return "*" * len(s)                              # 全打码——安全优先于可用性
    return s[:4] + "*" * (len(s) - 6) + s[-2:]           # 保留前4后2，中间打码

def mask_medical_no(no: str) -> str:                     # 病历号脱敏
    """病历号脱敏：仅保留末 4 位，供人工核对使用。

    入参：no 原始病历号。出参：形如 ****1234 的字符串。
    失败行为：长度不足 5 位时全打码。
    """
    if not no:                                           # 空值防御
        return ""                                        # 空进空出
    s = str(no)                                          # 统一为字符串
    if len(s) < 5:                                       # 过短编号全打码
        return "*" * len(s)                              # 全打码输出
    return "*" * (len(s) - 4) + s[-4:]                   # 保留末4位

_MASK_FUNCS = {                                          # 字段名 → 脱敏函数映射（规则来自 config.yaml）
    "name": mask_name,                                   # 姓名脱敏器
    "phone": mask_phone,                                 # 手机号脱敏器
    "id_card": mask_id_card,                             # 身份证脱敏器
    "medical_no": mask_medical_no,                       # 病历号脱敏器
}

def mask_record(record: dict) -> dict:                   # 按配置对单条业务记录脱敏
    """按 config.yaml 的 security.mask 规则对记录做字段级脱敏。

    入参：record 业务记录字典（浅拷贝脱敏，不改动调用方原数据）。
    出参：脱敏后的新字典。
    失败行为：非 dict 输入返回空 dict；未知字段名跳过不打码（避免误伤业务字段）。
    """
    if not isinstance(record, dict):                     # 类型防御
        return {}                                        # 非 dict 输入返回空
    settings = get_settings()                            # 读取脱敏字段配置
    rules: dict = settings.security.get("mask", {}) or {}  # 取出字段→类别映射
    out = dict(record)                                   # 浅拷贝：脱敏不污染原始数据（审计需保留可追溯源）
    for field, category in rules.items():                # 逐个配置字段处理
        if field in out and out[field] is not None:      # 字段存在且非空才处理
            func = _MASK_FUNCS.get(str(category))        # 查找对应脱敏函数
            if func:                                     # 已知类别才脱敏
                out[field] = func(str(out[field]))       # 执行脱敏——对外输出前必经的最后防线
    return out                                           # 返回脱敏副本

def mask_list(records: list) -> list:                    # 批量脱敏
    """对记录列表逐条脱敏。

    入参：records 记录字典列表。出参：脱敏后列表。
    失败行为：非 list 输入返回空列表。
    """
    if not isinstance(records, list):                    # 类型防御
        return []                                        # 非 list 返回空
    return [mask_record(r) for r in records]             # 逐条调用单记录脱敏
