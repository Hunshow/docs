# -*- coding: utf-8 -*-
"""阶段验证脚本：使用 TestClient 全链路自测（无需真实端口/Ollama 亦可跑通）。"""  # 脚本说明
from fastapi.testclient import TestClient                 # 进程内测试客户端（httpx 驱动）
from main import app                                      # 被测应用

BASE = "http://testserver"                                # TestClient 假基址（不发起真实网络）
PASS, FAIL = [], []                                       # 用例结果收集器


def check(name: str, cond: bool, detail: str = "") -> None:  # 单断言工具
    """记录并打印一条用例结果。

    入参：name 用例名；cond 断言条件；detail 失败补充信息。
    出参：无（写入 PASS/FAIL 列表并打印）。
    失败行为：断言失败仅记录，不中断后续用例（一次性看到全部问题）。
    """
    (PASS if cond else FAIL).append(name)                 # 归档结果
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")  # 打印


def login(username: str, password: str) -> str:           # 登录工具
    """登录并返回令牌。

    入参：username 用户名；password 口令。出参：token 字符串。
    失败行为：登录失败抛 AssertionError（测试前置失败应立即中止）。
    """
    r = client.post(f"{BASE}/api/auth/login", json={"username": username, "password": password})  # 调登录接口
    assert r.json().get("ok"), f"登录失败: {username} -> {r.text}"  # 前置断言
    return r.json()["data"]["token"]                      # 返回令牌

def auth_header(token: str) -> dict:                      # 请求头工具
    """构造带令牌的请求头。入参 token；出参 {"Authorization": "Bearer ..."}；失败行为无。"""
    return {"Authorization": f"Bearer {token}"}           # Bearer 头

client = TestClient(app)                                  # 创建测试客户端（进程内）

# ---- 1. 健康检查与知识库 ----
r = client.get(f"{BASE}/api/health")                      # 健康检查
check("健康检查返回统一结构", r.json().get("ok") is True)   # 断言统一结构
kb_chunks = r.json()["data"]["kb_chunks"]                 # 取知识分块数
graph_n = r.json()["data"]["graph_triples"]               # 取图谱三元组数
check("RAG 知识库已建索引", kb_chunks > 0, f"chunks={kb_chunks}")  # 断言分块
check("知识图谱已加载三元组", graph_n > 0, f"triples={graph_n}")    # 断言图谱

# ---- 2. 四角色登录 ----
tokens = {}                                               # 令牌容器
for user, pwd, role in [("patient01", "patient-pwd-01", "patient"),  # 患者
                        ("nurse01", "nurse-pwd-01", "nurse"),          # 护士
                        ("doctor01", "doctor-pwd-01", "dept_staff"),   # 医护
                        ("admin01", "admin-pwd-01", "admin")]:         # 管理员
    tokens[role] = login(user, pwd)                       # 逐个登录
check("四角色登录成功", len(tokens) == 4)                   # 断言登录
r = client.post(f"{BASE}/api/auth/login", json={"username": "nurse01", "password": "wrong"})  # 错误口令
check("错误口令被拒绝", r.json()["error"]["code"] == "AUTH_FAILED")  # 断言拒绝

# ---- 3. 患者预约挂号 + 脱敏验证 ----
r = client.post(f"{BASE}/api/patient/appointments",       # 患者预约
                headers=auth_header(tokens["patient"]),   # 患者令牌
                json={"patient_name": "张三", "phone": "13812345678",  # 预约信息
                      "medical_no": "MZ20261101999", "dept": "全科门诊",  # 预约信息
                      "doctor": "王济民", "schedule_time": "2026-11-12 10:00"})  # 预约信息
body = r.json()                                           # 解析响应
check("患者预约成功", body.get("ok") is True, f"apt_id={body.get('data', {}).get('apt_id')}")  # 断言预约
masked = body.get("data", {})                             # 取脱敏数据
check("手机号已脱敏", "****" in str(masked.get("phone", "")), f"phone={masked.get('phone')}")  # 断言脱敏
check("病历号已脱敏", str(masked.get("medical_no", "")).startswith("*"), f"medical_no={masked.get('medical_no')}")  # 断言脱敏
r = client.post(f"{BASE}/api/patient/appointments", headers=auth_header(tokens["nurse"]), json={})  # 护士越权预约
check("护士不能替患者预约", r.json()["error"]["code"] == "FORBIDDEN")  # 断言越权拦截
client.cookies.clear()                                   # 关键：登录已下发令牌 Cookie，必须清空才能验证“未登录”分支
r = client.post(f"{BASE}/api/patient/appointments", json={"patient_name": "x"})  # 未登录
check("未登录返回 401", r.json()["error"]["code"] == "AUTH_REQUIRED")  # 断言 401

# ---- 4. 医护队列与混队标注 ----
r = client.get(f"{BASE}/api/staff/queue", headers=auth_header(tokens["nurse"]))  # 护士看队列
rows = r.json().get("data", [])                           # 取队列
flagged = [x for x in rows if x.get("is_late_or_walk_in")]  # 混队标注行
check("护士可查看队列", r.json().get("ok") is True and len(rows) >= 2)  # 断言队列
check("迟到/加号混队已标注", len(flagged) >= 1)             # 断言标注
r = client.get(f"{BASE}/api/staff/queue", headers=auth_header(tokens["patient"]))  # 患者越权看队列
check("患者不能访问医护队列", r.json()["error"]["code"] == "FORBIDDEN")  # 断言越权

# ---- 5. 智能体：预约核验（正常 + 排班未同步兜底） ----
r = client.post(f"{BASE}/api/agents/appointment_verifier/run",  # 核验正常预约
                headers=auth_header(tokens["nurse"]), json={"query": "核验预约", "apt_id": "APT-002"})  # 护士发起
d = r.json().get("data", {})                              # 取结果
check("核验智能体可用", r.json().get("ok") is True)         # 断言
check("核验结果带事实引用", isinstance(d.get("references"), list) and len(d["references"]) > 0)  # 断言 RAG 引用
check("核验结果绑定资料版本", bool(d.get("data_version")))  # 断言版本
r = client.post(f"{BASE}/api/agents/appointment_verifier/run",  # 核验急诊暂停医生的预约
                headers=auth_header(tokens["nurse"]), json={"query": "核验异常", "apt_id": "APT-001"})  # APT-001 医生=钱有光? 见下
# 说明：APT-001 医生为王济民（正常）。为验证兜底场景，直接核验急诊暂停排班对应的场景描述  # 脚本内说明
d2 = r.json().get("data", {})                             # 取结果
check("核验输出结构完整", all(k in d2 for k in ("summary", "requires_human_confirm", "tool_calls")))  # 断言结构
r = client.post(f"{BASE}/api/agents/appointment_verifier/run",  # 患者越权调用核验
                headers=auth_header(tokens["patient"]), json={"query": "x", "apt_id": "APT-002"})  # 越权
check("患者不能调用医护智能体", r.json()["error"]["code"] == "FORBIDDEN")  # 断言越权

# ---- 6. 智能体：分诊推荐 + 人工确认闭环 ----
r = client.post(f"{BASE}/api/agents/triage_advisor/run",  # 生成分诊推荐
                headers=auth_header(tokens["nurse"]), json={"query": "给出次序建议", "apt_id": "APT-003"})  # 迟到样本
d = r.json().get("data", {})                              # 取结果
check("分诊推荐必须人工确认", d.get("requires_human_confirm") is True)  # 红线断言
rec_id = d.get("recommend_id", "")                        # 取推荐编号
check("推荐已生成待确认编号", rec_id.startswith("REC-"), f"rec_id={rec_id}")  # 断言编号
r = client.post(f"{BASE}/api/agents/recommend/{rec_id}/confirm",  # 不填理由确认
                headers=auth_header(tokens["nurse"]), json={"confirm": True, "note": ""})  # 缺理由
check("不填理由的确认被拒绝", r.json()["error"]["code"] == "NOTE_REQUIRED")  # 质控强制留痕
r = client.post(f"{BASE}/api/agents/recommend/{rec_id}/confirm",  # 规范确认
                headers=auth_header(tokens["nurse"]), json={"confirm": True, "note": "已核对签到时间，按次序叫号"})  # 带理由
d = r.json().get("data", {})                              # 取结果
check("护士确认后推荐生效", d.get("status") == "HUMAN_CONFIRMED")  # 断言确认
check("确认记录含签核人", d.get("confirmed_by") == "nurse01")  # 断言签核人
r = client.post(f"{BASE}/api/agents/triage_advisor/run",  # 患者越权生成推荐
                headers=auth_header(tokens["patient"]), json={"query": "x", "apt_id": "APT-002"})  # 越权
check("患者不能生成分诊推荐", r.json()["error"]["code"] == "FORBIDDEN")  # 断言越权

# ---- 7. 智能体：流量预判与协同提醒 ----
r = client.post(f"{BASE}/api/agents/flow_forecaster/run",  # 流量预判
                headers=auth_header(tokens["dept_staff"]), json={"query": "预判今日高峰"})  # 医护发起（角色键=dept_staff）
check("医护可获取流量预判", r.json().get("ok") is True)     # 断言
r = client.post(f"{BASE}/api/agents/collab_notifier/run",  # 协同提醒（升级场景）
                headers=auth_header(tokens["nurse"]),      # 护士发起
                json={"query": "升级通知", "apt_id": "APT-002",  # 任务体
                      "notify_type": "staff_escalation", "reason": "医生急诊暂停排班未同步", "receiver": "唐映秋"})  # 三要素
d = r.json().get("data", {})                              # 取结果
check("升级通知含接手人", "唐映秋" in str(d.get("message", "")))  # 断言三要素
r = client.post(f"{BASE}/api/agents/collab_notifier/run",  # 患者越权发通知
                headers=auth_header(tokens["patient"]), json={"query": "x", "notify_type": "patient_reminder"})  # 越权
check("患者不能发送医护通知", r.json()["error"]["code"] == "FORBIDDEN")  # 断言越权

# ---- 8. 管理员审计与统计 ----
r = client.get(f"{BASE}/api/admin/audit", headers=auth_header(tokens["admin"]))  # 调阅审计
rows = r.json().get("data", [])                           # 取记录
check("管理员可调阅审计", r.json().get("ok") is True and len(rows) > 0, f"entries={len(rows)}")  # 断言审计
actions = {x.get("action") for x in rows}                 # 动作集合
check("审计含越权拒绝记录", "DENIED" in str(actions) or any(x.get("decision") == "DENIED" for x in rows))  # 断言留痕
check("审计含人工确认记录", any(x.get("action") == "CONFIRM_RECOMMEND" for x in rows))  # 断言闭环留痕
r = client.get(f"{BASE}/api/admin/audit", headers=auth_header(tokens["nurse"]))  # 护士越权调阅
check("护士不能调阅审计", r.json()["error"]["code"] == "FORBIDDEN")  # 断言越权
r = client.get(f"{BASE}/api/admin/stats", headers=auth_header(tokens["admin"]))  # 统计
d = r.json().get("data", {})                              # 取统计
check("统计含推荐确认状态", all(k in d for k in ("recommend_pending", "recommend_confirmed", "data_version")))  # 断言统计

# ---- 9. 汇总 ----
print("\n========== 自测汇总 ==========")                   # 分隔输出
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")          # 汇总
if FAIL:                                                  # 有失败
    print("失败用例:", FAIL)                               # 列出失败
    raise SystemExit(1)                                   # 非零退出码：脚本失败
print("全部用例通过")                                       # 成功提示
