# -*- coding: utf-8 -*-
"""对外 API 路由：统一响应结构 + 接口级 RBAC 卡点 + 人工确认链路 + 全程审计留痕。"""  # 模块职责说明
import base64                                             # 诊断用：解析网关 JWT 载荷键名
import json                                               # 诊断用：解析 JWT 载荷
import re                                                 # 正则：诊断接口里对头部做脱敏形状化
import threading                                          # 推荐确认状态的并发锁
from typing import Dict, Any, Optional                    # 类型标注
from fastapi import APIRouter, Request, Body             # 路由、请求对象与原始请求体
from pydantic import BaseModel                            # 请求体模型
from app.response import ok_response, ok_json, err_response, err_body  # 统一响应（含带响应头版本）
from app.security.auth import (login, require_identity, get_identity,  # 鉴权、权限卡点、诊断用身份解析
                               extract_candidates, AUTH_COOKIE_NAME)  # 候选令牌抽取与 Cookie 名
from app.config import get_settings                       # 统一配置：读令牌有效期，不硬编码
from app.security.mask import mask_record, mask_list      # 脱敏出口
from app.security.audit import audit, read_audit          # 审计留痕
from app.services import store                            # 业务数据仓库
from app.hermes.agents import get_agents                  # 智能体装配

router_auth = APIRouter(prefix="/api/auth", tags=["auth"])          # 认证路由
router_patient = APIRouter(prefix="/api/patient", tags=["patient"]) # 患者路由
router_staff = APIRouter(prefix="/api/staff", tags=["staff"])       # 医护路由
router_agents = APIRouter(prefix="/api/agents", tags=["agents"])    # 智能体路由
router_admin = APIRouter(prefix="/api/admin", tags=["admin"])       # 管理员路由

_LOCK = threading.Lock()                                  # 推荐确认状态锁
_RECOMMENDATIONS: Dict[str, Dict[str, Any]] = {}          # 推荐暂存：recommend_id → 推荐+状态（人工确认链路）


def _guard(request: Request, action: str):                # 内部工具：统一鉴权卡点
    """接口统一入口卡点：鉴权失败转统一错误响应。

    入参：request 请求；action 动作标识。
    出参：(identity, None) 通过 / (None, 错误响应) 不通过。
    失败行为：AUTH_REQUIRED→401；FORBIDDEN→403（越权已在卡点内留痕）。
    """
    try:                                                  # 捕获卡点抛出的权限异常
        return require_identity(request, action), None    # 通过：返回身份
    except PermissionError as e:                          # 权限异常
        code = str(e)                                     # 取错误码
        if code == "AUTH_REQUIRED":                       # 未登录
            return None, err_response("AUTH_REQUIRED", "未登录或令牌无效", 401)  # 401
        return None, err_response("FORBIDDEN", "当前角色无权执行该操作", 403)  # 403（已留审计）


# ---------------- 认证 ----------------

class LoginBody(BaseModel):                               # 登录请求体
    """登录请求体模型。username 用户名；password 口令。"""
    username: str                                         # 用户名
    password: str                                         # 口令（仅传输，不落日志）

@router_auth.post("/login")                               # 登录接口
def api_login(body: LoginBody):                           # 处理登录
    """用户登录：校验口令并签发令牌，同时下发同名 Cookie 作为备用通道。

    入参：body{username,password}。出参：统一结构，data 含 token 与角色；响应头携带 HttpOnly Cookie。
    失败行为：口令错误返回 AUTH_FAILED（不区分账号/口令错，防枚举）。
    """
    result = login(body.username, body.password)          # 调用鉴权模块
    if result is None:                                    # 失败
        return err_response("AUTH_FAILED", "用户名或口令错误", 401)  # 401
    resp = ok_json(result)                                # 成功返回令牌（统一结构不变，仅换成可写响应头的对象）
    # 关键逻辑：为什么除了返回 token 还要写 Cookie
    # 部署在反向代理后面时，实测部分网关会剥离 Authorization 头，浏览器表现为“登录成功但所有业务接口 401”；
    # Cookie 由浏览器同源自动携带、不经过前端 JS，是网关环境下最稳的通道。
    # 这里设 HttpOnly 防 XSS 读取，SameSite=Lax 防 CSRF 跨站自动提交；
    # secure 暂为 False 以便本地 http 调试，正式上线（已启用 HTTPS）必须改 True，且应由网关统一强制 HTTPS
    resp.set_cookie(key=AUTH_COOKIE_NAME,                 # Cookie 名与后端取用常量保持一致
                    value=result["token"],                # 令牌本体（与响应体中的 token 完全相同）
                    httponly=True,                        # 禁止 JS 读取：降低 XSS 窃取风险
                    samesite="lax",                       # 同站请求才自动携带：降低 CSRF 风险
                    secure=False,                         # 本地 http 调试需要；生产环境应置 True
                    max_age=int(get_settings().security.get("token_expire_minutes", 480)) * 60,  # 与令牌有效期对齐
                    path="/")                             # 全站生效
    return resp                                           # 返回统一结构 + Set-Cookie

@router_auth.post("/logout")                              # 退出登录接口
def api_logout():                                         # 处理退出
    """退出登录：清除令牌 Cookie。

    入参：无。出参：统一结构，data 标记已退出。
    失败行为：无（Cookie 不存在时删除操作同样安全，不会报错）。
    """
    # 关键逻辑：为什么退出必须由后端删 Cookie
    # 令牌是无状态的 HMAC 签名串，服务端没有会话表可作废，唯一能“断掉”浏览器侧凭证的动作
    # 就是让浏览器删掉携带它的 Cookie；否则用户点了退出，Cookie 仍在，无头请求依然能被识别为已登录
    resp = ok_json({"logged_out": True})                  # 统一结构
    resp.delete_cookie(key=AUTH_COOKIE_NAME, path="/")    # 删除 Cookie（与服务端下发的属性保持一致）
    return resp                                           # 返回

@router_auth.get("/whoami")                               # 部署诊断接口：定位反向代理的凭证透传情况
def api_whoami(request: Request):                         # 回显“服务端实际收到了什么凭证通道”
    """诊断当前请求携带了哪些凭证通道（只回显名字与解析结果，绝不回显凭证值）。

    入参：request 请求对象。出参：统一结构，data 含 header 名清单、cookie 名清单、身份解析结果。
    失败行为：非调试模式返回 404；解析不出身份时 identity 为 None，不抛异常。
    """
    # 关键逻辑：为什么需要这个接口
    # 部署到网关后面时出现过“登录成功、业务接口全 401”，本地复现不了，只能让线上实例自己回显
    # 它到底收到了哪些 header / cookie 名，才能判断是网关剥离了凭证，还是凭证到了但验签失败。
    # 只回显“名字”和“是否解析成功”，不回显 token/cookie 的值，避免诊断接口本身变成泄露点。
    if not get_settings().app.get("debug", False):        # 生产环境默认关闭：避免暴露内部结构
        return err_response("NOT_ENABLED", "诊断接口仅在调试模式可用", 404)  # 关闭时视为不存在
    names = sorted({k.lower() for k in request.headers.keys()})  # 收到的 header 名（小写去重）
    raw_auth = request.headers.get("Authorization", "") or ""  # 原始授权头（只用于结构诊断，不落库）
    # 结构诊断：把所有长串（可能的令牌/编码串）替换为「长度占位符」，只保留分隔符与短前缀，
    # 这样能看到代理到底把头部改写成了什么形状，又不会把任何凭证内容回显出去。
    shape = re.sub(r"[A-Za-z0-9_\-=+/%]{8,}", lambda m: f"<SEG{len(m.group(0))}>", raw_auth)  # 脱敏后的形状串
    # 若网关把 Authorization 改写成了它自己的 JWT，这里只回显 JWT 载荷的“键名”，
    # 目的是判断原令牌是否被塞进了某个字段；键名不含内容，不构成泄露。
    jwt_keys: list = []                                   # JWT 载荷键名容器
    try:                                                  # 结构不符时静默跳过
        parts = raw_auth.split(" ", 1)[-1].split(".")      # 按空格与点号切分
        if len(parts) == 3:                               # 三段结构才按 JWT 解析
            pad = "=" * (-len(parts[1]) % 4)              # 补齐 base64 填充
            jwt_keys = sorted(json.loads(base64.urlsafe_b64decode(parts[1] + pad)).keys())  # 仅取键名
    except Exception:                                     # 非 JWT 或解码失败
        jwt_keys = []                                     # 保持空列表
    return ok_response({                                  # 统一结构返回
        "header_names": names,                            # header 名清单
        "cookie_names": sorted(request.cookies.keys()),    # cookie 名清单
        "auth_len": len(raw_auth),                         # 授权头长度：用于判断是否被截断/拼接
        "auth_scheme_ok": raw_auth.lower().startswith("bearer "),  # 方案名是否被代理改写（大小写）
        "auth_shape": shape[:200],                         # 脱敏后的头部形状（最多 200 字符）
        "jwt_payload_keys": jwt_keys,                      # 网关 JWT 载荷的键名（不含值）
        "candidates": len(extract_candidates(raw_auth)),   # 头部里被识别出的候选令牌个数
        "identity": get_identity(request),                 # 身份解析结果：非空说明某一通道可用
    })

# ---------------- 患者侧 ----------------
# 说明：预约接口改用原始 dict 接收请求体（鉴权先行），不再需要 Pydantic 请求体模型

def _strip_owner(rec: dict) -> dict:                     # 内部工具：剥离归属账号字段
    """剥离记录中的 owner 字段：归属账号是内部关联键，对患者端输出必须移除。

    入参：rec 预约记录。出参：不含 owner 的新字典（不改原数据）。
    失败行为：非 dict 返回空 dict。
    """
    if not isinstance(rec, dict):                        # 类型防御
        return {}                                        # 空进空出
    return {k: v for k, v in rec.items() if k != "owner"}  # 过滤内部字段——隐私红线：账号体系不外露

@router_patient.post("/appointments")                     # 预约挂号接口
def api_book(request: Request, payload: dict = Body(default={})):  # 用原始 dict：保证鉴权先于字段校验执行
    """患者预约挂号。

    入参：payload 预约信息（原始字典，缺失字段由仓库层校验）。
    出参：统一结构，data 为脱敏后的新预约记录。
    失败行为：未登录 401；越权 403；缺字段 400（VALIDATION_ERROR）。
    """
    identity, err = _guard(request, "book_appointment")   # 卡点：先鉴权再做字段校验——安全检查永远在前
    if err:                                               # 未通过
        return err                                        # 返回错误
    try:                                                  # 业务容错
        apt = store.create_appointment(payload, owner=identity["username"])  # 创建预约并绑定归属账号——保证"我的预约"能关联到本人
    except ValueError as e:                               # 缺必填字段
        return err_response("VALIDATION_ERROR", str(e), 400)  # 400
    audit(actor=identity["username"], role=identity["role"], action="BOOK",  # 预约动作留痕
          biz_id=apt["apt_id"], resource="appointment", data_version=store.get_data_version(),  # 绑定业务号与版本
          detail={"dept": apt["dept"], "doctor": apt["doctor"]}, decision="BOOKED")  # 明细与结论
    return ok_response(mask_record(_strip_owner(apt)))    # 出口脱敏并剥离内部归属字段

@router_patient.get("/appointments/mine")                 # 我的预约接口
def api_my_appointments(request: Request):                # 处理查询
    """患者查看本人预约（按归属账号或登录展示名匹配，演示口径）。

    入参：无（身份来自令牌）。出参：统一结构，data 为脱敏预约列表。
    失败行为：未登录 401；越权 403。
    """
    identity, err = _guard(request, "view_own_appointment")  # 卡点：患者本人
    if err:                                               # 未通过
        return err                                        # 返回错误
    rows = store.list_appointments()                      # 读全部预约
    # 关联口径：优先按预约归属账号（谁发起就归谁）；历史数据无归属时按展示名兜底——两种途径都不泄露他人记录
    mine = [r for r in rows if r.get("owner") == identity["username"]  # 归属账号匹配（新数据主口径）
            or (not r.get("owner") and r.get("patient_name") == identity.get("display_name"))]  # 无归属旧数据按姓名兜底
    audit(actor=identity["username"], role=identity["role"], action="VIEW_MINE",  # 调阅留痕
          biz_id="-", resource="appointment", decision="OK")  # 调阅也要留痕
    return ok_response(mask_list([_strip_owner(r) for r in mine]))  # 出口脱敏并剥离内部归属字段

@router_patient.get("/queue/position/{apt_id}")           # 候诊进度接口
def api_queue_position(apt_id: str, request: Request):    # 处理查询
    """患者查看候诊进度（前方可视，不做实时承诺）。

    入参：apt_id 预约号。出参：统一结构，data 为位次与更新提示。
    失败行为：未登录 401；不在队列时 data.position 为 null 并提示。
    """
    identity, err = _guard(request, "view_queue_position")  # 卡点：患者或护士
    if err:                                               # 未通过
        return err                                        # 返回错误
    q = store._load().get("queue", [])                    # 读队列快照
    ordered = sorted(q, key=lambda r: str(r.get("check_in_time", "")))  # 签到时间排序
    pos = next((i + 1 for i, r in enumerate(ordered) if r.get("apt_id") == apt_id), None)  # 计算位次
    audit(actor=identity["username"], role=identity["role"], action="VIEW_POSITION",  # 留痕
          biz_id=apt_id, resource="queue", decision="OK")  # 绑定业务号
    return ok_response({"apt_id": apt_id, "position": pos,          # 位次（可为 null）
                        "notice": "信息每日两次更新，超过更新周期标记待刷新，不作实时承诺"})  # 口径声明

# ---------------- 医护侧 ----------------

@router_staff.get("/queue")                               # 队列查看接口
def api_queue(request: Request, dept: Optional[str] = None):  # 处理查询
    """医护查看叫号队列（含迟到/加号混队标注）。

    入参：dept 可选科室过滤。出参：统一结构，data 为队列列表（预约号级信息，无个人敏感字段）。
    失败行为：未登录 401；越权 403。
    """
    identity, err = _guard(request, "view_queue")         # 卡点：护士/医护/管理员
    if err:                                               # 未通过
        return err                                        # 返回错误
    q = store._load().get("queue", [])                    # 读队列
    if dept:                                              # 科室过滤
        q = [r for r in q if r.get("dept") == dept]       # 过滤
    appts = {a["apt_id"]: a for a in store.list_appointments()}  # 预约索引
    rows = []                                             # 输出行
    for r in q:                                           # 组装输出
        a = appts.get(r.get("apt_id"), {})                # 关联预约
        rows.append({"apt_id": r.get("apt_id"), "dept": r.get("dept"),  # 基础字段
                     "position": r.get("position"), "check_in_time": r.get("check_in_time"),  # 位次与签到
                     "is_late_or_walk_in": a.get("status") == "late_arrival" or a.get("source") == "walk_in",  # 混队标注
                     "doctor": a.get("doctor", "-")})     # 医生
    audit(actor=identity["username"], role=identity["role"], action="VIEW_QUEUE",  # 留痕
          resource="queue", decision="OK")                # 记录调阅
    return ok_response(rows)                              # 返回（无敏感字段，无需脱敏）

@router_staff.get("/forecast")                            # 流量预警接口
def api_forecast(request: Request):                       # 处理查询
    """医护查看流量预警（调流量预判智能体）。

    入参：无。出参：统一结构，data 为预判结果。
    失败行为：未登录 401；越权 403。
    """
    identity, err = _guard(request, "view_forecast")      # 卡点
    if err:                                               # 未通过
        return err                                        # 返回
    agents = get_agents()                                 # 装配智能体
    result = agents["flow_forecaster"].run({"query": "今日候诊流量预判"})  # 执行预判
    audit(actor=identity["username"], role=identity["role"], action="FORECAST",  # 留痕
          resource="forecast", data_version=result.get("data_version"),  # 绑定版本
          detail={"references": result.get("references", [])}, decision="OK")  # 事实引用入审计
    return ok_response(result)                            # 返回

class FeedbackBody(BaseModel):                            # 异常反馈请求体
    """异常反馈请求体：apt_id 关联预约；reason 异常说明（可为空，留空也留痕）。"""
    apt_id: str                                           # 关联预约号
    reason: str = ""                                      # 异常说明

@router_staff.post("/feedback")                           # 科室医护反馈分诊异常
def api_feedback(body: FeedbackBody, request: Request):   # 处理反馈
    """科室医护反馈分诊异常（只登记待审核，不直接改动任何业务数据）。

    入参：body{apt_id,reason}。出参：统一结构，data 为反馈记录（状态 PENDING_REVIEW）。
    失败行为：未登录 401；越权 403（dept_staff 权限矩阵内已登记 feedback_anomaly）。
    """
    identity, err = _guard(request, "feedback_anomaly")   # 卡点：护士/医护可反馈异常
    if err:                                               # 未通过
        return err                                        # 返回错误
    fb_id = f"FB-{body.apt_id}"                           # 反馈编号：业务号派生，便于回溯
    audit(actor=identity["username"], role=identity["role"], action="FEEDBACK_ANOMALY",  # 反馈留痕
          biz_id=body.apt_id, resource=fb_id,             # 绑定业务号与反馈号
          data_version=store.get_data_version(),          # 记录当时的资料版本
          detail={"reason": body.reason}, decision="PENDING_REVIEW")  # 结论：待管理员审核
    # 注意：这里只登记，不修改分诊结果或诊疗数据——红线卡点，终审权在人工
    return ok_response({"feedback_id": fb_id, "apt_id": body.apt_id,  # 返回反馈记录
                        "status": "PENDING_REVIEW",       # 待审核状态
                        "note": "已登记，转管理员审核；系统未改动任何分诊结果"})  # 明确口径

# ---------------- 智能体侧 ----------------

AGENT_ACTIONS = {                                         # 智能体 → 所需权限动作映射（RBAC 二级卡点）
    "appointment_verifier": "verify_appointment",         # 核验：护士/管理员
    "flow_forecaster": "view_forecast",                   # 预判：医护/管理员
    "triage_advisor": "view_recommend",                   # 推荐：护士/医护/管理员
    "collab_notifier": "send_notify",                     # 通知：护士
}

class AgentRunBody(BaseModel):                            # 智能体任务体
    """智能体任务体：query 为自然语言任务；其余键随智能体而定（如 apt_id）。"""
    query: str                                            # 任务描述
    apt_id: Optional[str] = None                          # 关联预约号（可选）
    notify_type: Optional[str] = None                     # 通知类型（协同提醒用）
    reason: Optional[str] = None                          # 升级原因（协同提醒用）
    receiver: Optional[str] = None                        # 接手人（协同提醒用）

@router_agents.post("/{agent_name}/run")                  # 智能体执行接口
def api_agent_run(agent_name: str, body: AgentRunBody, request: Request):  # 处理执行
    """执行指定智能体任务，产出建议（含事实引用与资料版本）。

    入参：agent_name 智能体名；body 任务体。出参：统一结构，data 为建议结果。
    失败行为：未知智能体 404（AGENT_NOT_FOUND）；未登录 401；越权 403。
    """
    agents = get_agents()                                 # 装配智能体
    agent = agents.get(agent_name)                        # 查实例
    if agent is None:                                     # 未知智能体
        return err_response("AGENT_NOT_FOUND", f"智能体不存在: {agent_name}", 404)  # 404
    action = AGENT_ACTIONS.get(agent_name, "view_recommend")  # 查所需权限
    identity, err = _guard(request, action)               # 权限卡点
    if err:                                               # 未通过
        return err                                        # 返回
    task = {"query": body.query, "apt_id": body.apt_id,   # 组装任务
            "notify_type": body.notify_type, "reason": body.reason, "receiver": body.receiver}  # 透传字段
    result = agent.run(task)                              # Hermes 循环执行
    biz_id = body.apt_id or "-"                           # 业务编号
    audit(actor=identity["username"], role=identity["role"], action=f"AGENT_{agent_name}",  # 智能体动作留痕
          biz_id=biz_id, resource=agent_name, data_version=result.get("data_version"),  # 绑定版本
          detail={"summary": result.get("summary"), "references": result.get("references", [])},  # 摘要与事实引用
          decision="PENDING_HUMAN" if result.get("requires_human_confirm") else "AUTO_OK")  # 结论状态
    # 分诊推荐单独入暂存，等待人工确认——错误结果不能覆盖原记录
    if agent_name == "triage_advisor" and result.get("recommendation"):  # 有推荐才暂存
        with _LOCK:                                       # 加锁保护暂存
            _RECOMMENDATIONS[result["recommend_id"]] = {  # 记录推荐
                "recommend_id": result["recommend_id"],   # 推荐编号
                "apt_id": body.apt_id,                    # 关联预约
                "recommendation": result["recommendation"],  # 推荐内容
                "summary": result["summary"],             # 摘要
                "status": "PENDING_HUMAN",                # 状态：待人工确认
                "proposed_by": agent_name,                # 提出方
                "data_version": result.get("data_version"),  # 资料版本
            }
    return ok_response(result)                            # 返回建议

class ConfirmBody(BaseModel):                             # 人工确认请求体
    """人工确认请求体：confirm 为 True 确认 / False 驳回；note 为人工改动说明。"""
    confirm: bool                                         # 确认或驳回
    note: str = ""                                        # 人工改动/驳回理由（质控要求强制填写）

@router_agents.post("/recommend/{recommend_id}/confirm")  # 人工确认接口
def api_confirm_recommend(recommend_id: str, body: ConfirmBody, request: Request):  # 处理确认
    """护士对分诊推荐做人工确认/驳回（唯一生效路径）。

    入参：recommend_id 推荐编号；body{confirm,note}。出参：统一结构，data 为更新后的推荐。
    失败行为：推荐不存在 404；未填写 note 返回 400（改动必须留理由——质控要求）；越权 403。
    """
    identity, err = _guard(request, "confirm_recommend")  # 卡点：仅护士/管理员可确认
    if err:                                               # 未通过
        return err                                        # 返回
    if not body.note.strip():                             # 未填说明
        return err_response("NOTE_REQUIRED", "人工确认必须填写改动/驳回理由（质控留痕要求）", 400)  # 强制留痕
    with _LOCK:                                           # 加锁读改写
        rec = _RECOMMENDATIONS.get(recommend_id)          # 取推荐
        if rec is None:                                   # 不存在
            return err_response("RECOMMEND_NOT_FOUND", f"推荐不存在: {recommend_id}", 404)  # 404
        rec["status"] = "HUMAN_CONFIRMED" if body.confirm else "REJECTED"  # 更新状态
        rec["confirmed_by"] = identity["username"]        # 签核人（审计要素）
        rec["human_note"] = body.note                     # 人工改动说明（审计要素）
        rec["confirm_time"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")  # 确认时间（审计要素）
        # 明确写回原记录的只有推荐状态，绝不直接改队列次序或诊疗数据——红线卡点
    audit(actor=identity["username"], role=identity["role"], action="CONFIRM_RECOMMEND",  # 留痕
          biz_id=str(rec.get("apt_id", "-")), resource=recommend_id,       # 业务号与推荐号
          data_version=rec.get("data_version"),                            # 资料版本
          detail={"confirm": body.confirm, "note": body.note},             # 人工改动明细
          decision=rec["status"])                                          # 结论
    return ok_response(rec)                               # 返回更新后的推荐

# ---------------- 管理员侧 ----------------

@router_admin.get("/audit")                               # 审计调阅接口
def api_audit(request: Request, biz_id: Optional[str] = None, limit: int = 100):  # 处理调阅
    """管理员/质控调阅审计记录（调阅本身也留痕）。

    入参：biz_id 可选业务编号过滤；limit 条数上限。出参：统一结构，data 为审计记录列表。
    失败行为：未登录 401；越权 403。
    """
    identity, err = _guard(request, "view_audit")         # 卡点：仅管理员
    if err:                                               # 未通过
        return err                                        # 返回
    rows = read_audit(biz_id=biz_id, limit=limit)         # 读审计
    audit(actor=identity["username"], role=identity["role"], action="READ_AUDIT",  # 调阅留痕——调阅本身可追溯
          biz_id=biz_id or "-", resource="audit", decision="OK")  # 记录
    return ok_response(rows)                              # 返回

@router_admin.get("/stats")                               # 统计接口
def api_stats(request: Request):                          # 处理统计
    """管理员查看系统统计（预约量/队列量/推荐确认状态）。

    入参：无。出参：统一结构，data 为统计字典。
    失败行为：未登录 401；越权 403。
    """
    identity, err = _guard(request, "view_stats")         # 卡点
    if err:                                               # 未通过
        return err                                        # 返回
    appts = store.list_appointments()                     # 读预约
    with _LOCK:                                           # 加锁读推荐
        recs = list(_RECOMMENDATIONS.values())            # 快照
    stats = {                                             # 统计结果
        "appointment_total": len(appts),                  # 预约总量
        "no_show_total": sum(1 for a in appts if a.get("status") == "no_show"),  # 爽约量（口径：预约未到且未取消）
        "late_or_walk_in_total": sum(1 for a in appts if a.get("status") == "late_arrival" or a.get("source") == "walk_in"),  # 迟到加号量
        "recommend_pending": sum(1 for r in recs if r.get("status") == "PENDING_HUMAN"),  # 待人工确认
        "recommend_confirmed": sum(1 for r in recs if r.get("status") == "HUMAN_CONFIRMED"),  # 已确认
        "recommend_rejected": sum(1 for r in recs if r.get("status") == "REJECTED"),  # 已驳回
        "data_version": store.get_data_version(),         # 当前资料版本
    }
    audit(actor=identity["username"], role=identity["role"], action="VIEW_STATS",  # 留痕
          resource="stats", decision="OK")                # 记录
    return ok_response(stats)                             # 返回
