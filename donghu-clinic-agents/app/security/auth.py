# -*- coding: utf-8 -*-
"""鉴权与 RBAC 权限模块：登录签发令牌、接口级角色校验，禁止借用账号越权访问。"""  # 模块职责说明
import hmac                                              # HMAC 签名（标准库，无需外部 JWT 依赖）
import hashlib                                           # 口令摘要
import base64                                            # 令牌编解码
import json                                              # 载荷序列化
import re                                                # 正则：从被代理改写的头部里抽取令牌
import time                                              # 过期判断
from fastapi import Request                              # 请求对象：提取令牌
from typing import Optional, Dict, Any, List             # 类型标注
from app.config import get_settings                      # 统一配置
from app.security.audit import audit                     # 审计留痕


def _b64e(data: bytes) -> str:                           # 内部工具：字节转 URL 安全 base64
    """字节转 URL 安全 base64 字符串。入参 bytes；出参 str；失败行为：无。"""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")  # 编码并去填充符

def _b64d(s: str) -> bytes:                              # 内部工具：URL 安全 base64 转字节
    """URL 安全 base64 转字节。入参 str；出参 bytes；失败行为：非法字符抛异常，由调用方捕获。"""
    pad = "=" * (-len(s) % 4)                            # 补齐 base64 填充
    return base64.urlsafe_b64decode(s + pad)             # 解码

def hash_password(password: str) -> str:                 # 口令摘要
    """口令单向摘要：SHA256(密钥 + 口令)，演示实现，生产须换 bcrypt 等慢哈希。

    入参：password 明文口令。出参：十六进制摘要字符串。
    失败行为：无。摘要不可逆，不存储明文。
    """
    secret = get_settings().security["secret_key"]       # 从配置读密钥（禁止写死在代码里）
    return hashlib.sha256((secret + password).encode("utf-8")).hexdigest()  # 加盐摘要

def login(username: str, password: str) -> Optional[Dict[str, Any]]:  # 登录校验并签发令牌
    """校验用户名口令，成功则签发 HMAC 签名令牌。

    入参：username 用户名；password 明文口令。
    出参：成功返回 {"token":..., "username":..., "role":..., "display_name":...}；失败返回 None。
    失败行为：账号不存在/口令错误/账号被禁用均返回 None，不区分原因（防止账号枚举——安全考虑）。
    """
    settings = get_settings()                            # 读取配置
    user = None                                          # 命中的账号容器
    for u in settings.demo_users:                        # 遍历演示账号表
        if u.get("username") == username:                # 找到同名账号
            user = u                                     # 记录
            break                                        # 停止遍历
    if user is None:                                     # 账号不存在
        return None                                      # 统一返回失败
    if hash_password(password) != hash_password(user.get("password", "")):  # 口令比对（恒时比较见下）
        return None                                      # 口令错误：拒绝
    now = int(time.time())                               # 当前时间戳（秒）
    expire = now + int(settings.security.get("token_expire_minutes", 480)) * 60  # 过期时间戳
    payload = {"username": username, "role": user.get("role"),  # 载荷：身份与角色
               "display_name": user.get("display_name", ""),    # 展示名（输出时再脱敏）
               "iat": now, "exp": expire}                    # 签发与过期时间
    body = _b64e(json.dumps(payload, ensure_ascii=False).encode("utf-8"))  # 载荷编码
    secret = settings.security["secret_key"].encode("utf-8")  # 签名密钥
    sig = _b64e(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())  # HMAC-SHA256 签名
    token = f"{body}.{sig}"                              # 令牌 = 载荷.签名
    audit(actor=username, role=user.get("role", "-"), action="LOGIN",  # 登录动作留痕
          detail={"ip_hint": "local"})                   # 明细：本地演示无真实 IP
    return {"token": token, "username": username,        # 返回令牌与身份信息
            "role": user.get("role"), "display_name": user.get("display_name", "")}

def verify_token(token: str) -> Optional[Dict[str, Any]]:  # 校验令牌
    """校验令牌签名与有效期。

    入参：token 登录时签发的令牌字符串。
    出参：有效返回载荷 dict（含 username/role）；无效/过期/被篡改返回 None。
    失败行为：任何解析或签名异常均返回 None——宁可拒绝也不能放行伪造身份。
    """
    if not token or "." not in token:                    # 格式预检
        return None                                      # 非法格式直接拒绝
    try:                                                 # 解析与验签全程容错
        body, sig = token.split(".", 1)                  # 拆分载荷与签名
        secret = get_settings().security["secret_key"].encode("utf-8")  # 读签名密钥
        expect = _b64e(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())  # 期望签名
        if not hmac.compare_digest(sig, expect):         # 恒时比较防止时序侧信道——安全校验关键点
            return None                                  # 签名不符：令牌被篡改
        payload = json.loads(_b64d(body).decode("utf-8"))  # 解码载荷
        if int(payload.get("exp", 0)) < int(time.time()):  # 过期检查
            return None                                  # 已过期拒绝
        return payload                                   # 返回身份载荷
    except Exception:                                    # 任何异常都视为令牌无效
        return None                                      # 统一拒绝

AUTH_COOKIE_NAME = "app_token"                           # 令牌 Cookie 名：反向代理剥离 Authorization 头时的兜底通道
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}")  # 令牌形态：载荷.签名（均为 URL 安全 base64）

def extract_candidates(raw: str) -> List[str]:           # 从任意头部值里抽取可能的令牌串
    """从头部原值中抽取所有形如“载荷.签名”的候选令牌。

    入参：raw 头部原始字符串（可能被代理改写、拼接或加前缀）。
    出参：候选令牌列表（按出现顺序）；无匹配返回空列表。
    失败行为：不抛异常；任何异常输入都按“没有候选”处理。
    """
    # 关键逻辑：为什么要“抽取”而不是简单切掉 Bearer 前缀
    # 实测部署到网关后面时，Authorization 头长度是本地直连时的两倍（386 vs 194），
    # 说明代理对头部做了拼接/改写，直接按 "Bearer " 切片会得到脏串，验签必然失败。
    # 这里用正则把每个候选令牌单独抠出来逐个验签——安全上没有任何放松：
    # 候选再多也必须过 HMAC 验签才能通过，伪造的令牌不会因为“在候选列表里”而被接受。
    if not raw:                                          # 空值
        return []                                        # 无候选
    return _TOKEN_PATTERN.findall(raw)                   # 抽取全部候选

def tokens_from_proxy_jwt(raw: str) -> List[str]:        # 从代理注入的 JWT 里取出承载的原始令牌
    """解析 Authorization 中的 JWT 载荷，取出网关承载的原始令牌。

    入参：raw 授权头原值（形如 "Bearer <header>.<payload>.<sig>"）。
    出参：候选令牌列表（通常 0 或 1 个）。
    失败行为：结构不是三段 JWT、或载荷里没有 token 字段时返回空列表，不抛异常。
    """
    # 关键逻辑：为什么要解代理的 JWT
    # 实测部署网关会把客户端送来的 Authorization 整个替换成它自己的 JWT：
    #   Bearer <36>.<298>.<43>，而原始令牌被放在该 JWT 载荷的 token 字段里。
    # 这里只做“取回原始凭证”，并不信任 JWT 里的 user_id 等声明；
    # 取出的令牌仍要过本项目的 HMAC 验签与有效期校验，所以伪造 JWT 也拿不到任何权限。
    try:                                                 # 全程容错：结构不符即视为没有候选
        parts = raw.split(" ", 1)[-1].split(".")         # 去掉 Bearer 前缀后按点号切分
        if len(parts) != 3:                              # 不是三段 JWT
            return []                                    # 无候选
        pad = "=" * (-len(parts[1]) % 4)                 # 补齐 base64 填充
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))  # 解析载荷
        inner = payload.get("token")                     # 取出网关承载的原始令牌
        return [inner] if isinstance(inner, str) and inner else []  # 只接受非空字符串
    except Exception:                                    # 解码/解析失败
        return []                                        # 无候选

def get_identity(request: Request) -> Optional[Dict[str, Any]]:  # 从请求提取身份
    """从请求头或 Cookie 提取并校验身份，按 Authorization → X-Auth-Token → Cookie 顺序回退。

    入参：request FastAPI 请求对象。
    出参：有效返回 {"username","role","display_name"}；未携带/无效返回 None。
    失败行为：所有通道的所有候选令牌都验签失败时返回 None，由调用方决定 401。
    """
    # 关键逻辑：为什么要有三个通道
    # 1) 本地直连时前端只用 Authorization，这是标准做法；
    # 2) 部署到反向代理后面时，代理会拼接/改写 Authorization 头（实测长度翻倍），
    #    所以先把头部里所有形如令牌的串都抽出来，逐个验签，而不是死板地按前缀切片；
    # 3) 另外保留自定义头与 Cookie 两条兜底通道，应对代理直接剥离 Authorization 的情况；
    # 4) 所有通道、所有候选共用同一个 verify_token，绝不因为“换了个通道”而跳过签名校验。
    candidates: List[str] = []                           # 候选令牌收集器
    auth = (request.headers.get("Authorization", "") or "").strip()  # 通道一：标准 Authorization 头
    candidates.extend(extract_candidates(auth))          # 抽取该头里的候选令牌（兼容代理拼接）
    candidates.extend(tokens_from_proxy_jwt(auth))       # 代理把令牌包进它自己的 JWT 时，从载荷里取回
    if not candidates and auth and "." in auth:          # 头部完全不含标准形态时
        candidates.append(auth)                          # 整段当作候选，仍然要过验签
    candidates.extend(extract_candidates(request.headers.get("X-Auth-Token", "") or ""))  # 通道二：自定义头
    candidates.extend(extract_candidates(request.cookies.get(AUTH_COOKIE_NAME, "") or ""))  # 通道三：Cookie
    for token in candidates:                             # 逐个候选验签
        payload = verify_token(token)                    # 验签与过期校验（唯一放行依据）
        if payload is not None:                          # 验签通过
            return {"username": payload.get("username", ""),  # 组装干净的身份对象
                    "role": payload.get("role", ""),      # 角色
                    "display_name": payload.get("display_name", "")}  # 展示名
    return None                                          # 全部失败：视为未登录

# 角色 → 可访问动作白名单：权限矩阵集中在这一处，新增接口必须先在这里登记（RBAC 卡点）
ROLE_ACTIONS: Dict[str, set] = {                         # 动作粒度的角色权限矩阵
    "patient":   {"book_appointment", "view_own_appointment", "view_queue_position", "receive_notify"},  # 患者动作
    "nurse":     {"view_queue", "verify_appointment", "view_recommend", "confirm_recommend", "send_notify", "view_queue_position", "feedback_anomaly"},  # 分诊/护士动作
    "dept_staff":{"view_queue", "view_assigned_queue", "view_recommend", "feedback_anomaly", "view_forecast"},  # 科室医护动作
    "admin":     {"manage_rules", "view_audit", "view_stats", "manage_users", "review_anomaly", "view_queue", "view_recommend", "view_forecast"},  # 管理员动作
}

def check_permission(role: str, action: str) -> bool:    # 权限判定
    """判定某角色是否可执行某动作。

    入参：role 角色标识；action 动作标识。
    出参：True 允许 / False 拒绝。
    失败行为：未知角色或未知动作一律 False——默认拒绝，白名单之外全禁止。
    """
    return action in ROLE_ACTIONS.get(role, set())       # 默认拒绝原则：查不到即禁止

def require_identity(request: Request, action: str) -> Dict[str, Any]:  # 接口级统一卡点
    """接口级鉴权卡点：验身份 + 查权限矩阵 + 失败留痕。

    入参：request 请求对象；action 本接口对应的动作标识。
    出参：通过返回身份 dict；不通过抛 PermissionError（由路由层转 401/403）。
    失败行为：未登录抛 PermissionError("AUTH_REQUIRED")；越权抛 PermissionError("FORBIDDEN") 并留审计。
    """
    identity = get_identity(request)                     # 先验身份
    if identity is None:                                 # 未登录
        raise PermissionError("AUTH_REQUIRED")           # 抛认证错误（401）
    if not check_permission(identity["role"], action):   # 再查权限矩阵——RABC 唯一判定入口
        audit(actor=identity["username"], role=identity["role"],  # 越权尝试必须留痕——质控要求
              action=action, decision="DENIED",          # 记录动作与拒绝结论
              detail={"reason": "越权访问被拒绝"})        # 明细说明
        raise PermissionError("FORBIDDEN")               # 抛授权错误（403）
    return identity                                      # 通过：返回身份供业务使用
