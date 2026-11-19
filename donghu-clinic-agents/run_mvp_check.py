# -*- coding: utf-8 -*-
"""MVP 演示自测：界面可达性 + 四角色主链路 + 新增异常反馈接口 + 权限卡点。

用法：python run_mvp_check.py
出参：逐条打印 PASS/FAIL，末尾打印汇总；有失败时退出码为 1。
失败行为：任何断言失败只记录不抛栈，便于一次看全所有问题。
"""
import sys                                                   # 退出码
from fastapi.testclient import TestClient                    # 进程内测试客户端
from main import app                                         # 复用应用工厂产物

client = TestClient(app)                                     # 构造客户端
PASS, FAIL = [], []                                          # 结果收集器


def check(name: str, cond: bool, extra: str = "") -> None:    # 断言工具
    """记录一条断言结果。

    入参：name 用例名；cond 是否通过；extra 附加信息。
    出参：无。失败行为：不抛异常，仅记录。
    """
    (PASS if cond else FAIL).append(name)                    # 归入对应集合
    print(("[PASS] " if cond else "[FAIL] ") + name + (("  -> " + extra) if extra and not cond else ""), flush=True)


def login(username: str, password: str) -> str:              # 登录取令牌
    """登录并返回令牌。

    入参：账号、口令。出参：令牌字符串（失败返回空串）。
    失败行为：登录失败返回空串，由调用方断言。
    """
    r = client.post("/api/auth/login", json={"username": username, "password": password})  # 调登录
    j = r.json()                                             # 解析统一结构
    return j["data"]["token"] if j.get("ok") else ""          # 返回令牌或空串


def hdr(token: str) -> dict:                                 # 组装鉴权头
    """生成 Bearer 鉴权头。入参 token；出参 dict；失败行为：无。"""
    return {"Authorization": "Bearer " + token}              # Bearer 方案


# ---------------- 1. 界面可达性 ----------------
r = client.get("/ui/")                                       # 静态首页
check("GET /ui/ 返回 200", r.status_code == 200, str(r.status_code))
check("首页含系统标题", "东湖门诊中心" in r.text)
check("首页引用本地样式", "./styles.css" in r.text)

r = client.get("/ui/app.js")                                 # 脚本文件
check("GET /ui/app.js 返回 200", r.status_code == 200, str(r.status_code))
check("脚本含统一响应处理", "j.ok" in r.text)

r = client.get("/", follow_redirects=False)                   # 根路径跳转
check("GET / 跳转 /ui/", r.status_code in (302, 307) and "/ui/" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location')}")

# ---------------- 2. 四角色登录 ----------------
t_patient = login("patient01", "patient-pwd-01")             # 患者
t_nurse = login("nurse01", "nurse-pwd-01")                   # 护士
t_doctor = login("doctor01", "doctor-pwd-01")                # 医护
t_admin = login("admin01", "admin-pwd-01")                   # 管理员
check("四角色均能登录", all([t_patient, t_nurse, t_doctor, t_admin]))

# ---------------- 3. 患者：预约 + 我的预约 ----------------
r = client.post("/api/patient/appointments", headers=hdr(t_patient), json={  # 新建预约
    "patient_name": "张三", "phone": "13812345678", "medical_no": "MZ20261101001",
    "dept": "全科门诊", "doctor": "王济民", "schedule_time": "2026-11-19 10:00"})
j = r.json()
check("患者预约成功", j.get("ok") is True, str(j.get("error")))
check("预约返回已脱敏（手机号打码）", "****" in str(j["data"].get("phone", "")), str(j["data"].get("phone")))

r = client.get("/api/patient/appointments/mine", headers=hdr(t_patient))  # 我的预约
j = r.json()
check("我的预约可读且脱敏", j.get("ok") is True and "****" in str(j["data"]))

# ---------------- 4. 护士：核验 → 推荐 → 人工确认 ----------------
r = client.post("/api/agents/appointment_verifier/run", headers=hdr(t_nurse),
                json={"query": "核验", "apt_id": "APT-002"})
j = r.json()
check("核验智能体可执行", j.get("ok") is True, str(j.get("error")))
check("核验结果带事实引用", bool(j["data"].get("references")))
check("核验结果带资料版本", bool(j["data"].get("data_version")))

r = client.post("/api/agents/triage_advisor/run", headers=hdr(t_nurse),
                json={"query": "推荐", "apt_id": "APT-003"})
j = r.json()
rec_id = j["data"].get("recommend_id") if j.get("ok") else None
check("推荐智能体可执行", j.get("ok") is True, str(j.get("error")))
check("推荐恒为待人工确认", j["data"].get("requires_human_confirm") is True)

r = client.post(f"/api/agents/recommend/{rec_id}/confirm", headers=hdr(t_nurse),
                json={"confirm": True, "note": ""})
check("无理由确认被拒（400）", r.json().get("ok") is False and r.status_code == 400)

r = client.post(f"/api/agents/recommend/{rec_id}/confirm", headers=hdr(t_nurse),
                json={"confirm": True, "note": "护士核对签到时间后确认次序"})
j = r.json()
check("填理由后可确认", j.get("ok") is True and j["data"]["status"] == "HUMAN_CONFIRMED")
check("确认留签核人", j["data"].get("confirmed_by") == "nurse01")

# ---------------- 5. 医护：队列 / 预判 / 异常反馈 ----------------
r = client.get("/api/staff/queue", headers=hdr(t_doctor))
j = r.json()
check("医护可看队列", j.get("ok") is True)
check("队列含混队标注字段", "is_late_or_walk_in" in (j["data"][0] if j["data"] else {}))

r = client.get("/api/staff/forecast", headers=hdr(t_doctor))
check("流量预判可执行", r.json().get("ok") is True)

r = client.post("/api/staff/feedback", headers=hdr(t_doctor),
                json={"apt_id": "APT-003", "reason": "疑似排班未同步，建议人工核对"})
j = r.json()
check("医护异常反馈可提交", j.get("ok") is True and j["data"]["status"] == "PENDING_REVIEW")

# ---------------- 6. 权限卡点 ----------------
r = client.post("/api/staff/feedback", headers=hdr(t_patient), json={"apt_id": "APT-003", "reason": "x"})
check("患者反馈异常被拒（403）", r.status_code == 403)

client.cookies.clear()                                   # 关键：登录会下发令牌 Cookie，这里必须清空才能构造真正的“未登录”场景
r = client.post("/api/staff/feedback", json={"apt_id": "APT-003", "reason": "x"})
check("未登录访问被拒（401）", r.status_code == 401)

r = client.get("/api/admin/audit", headers=hdr(t_doctor))
check("医护调阅审计被拒（403）", r.status_code == 403)

# ---------------- 7. 管理员：统计 / 审计 ----------------
r = client.get("/api/admin/stats", headers=hdr(t_admin))
j = r.json()
check("管理员统计可读", j.get("ok") is True)
check("统计含待确认计数", "recommend_pending" in (j["data"] or {}))

r = client.get("/api/admin/audit", headers=hdr(t_admin), params={"limit": 200})
rows = r.json().get("data", [])
actions = {x.get("action") for x in rows}
check("审计可调阅", len(rows) > 0)
check("审计含异常反馈记录", "FEEDBACK_ANOMALY" in actions)
check("审计含人工确认记录", "CONFIRM_RECOMMEND" in actions)

# ---------------- 汇总 ----------------
print("", flush=True)
print(f"合计 {len(PASS) + len(FAIL)} 项：PASS={len(PASS)}  FAIL={len(FAIL)}", flush=True)
sys.exit(0 if not FAIL else 1)                               # 有失败则非 0 退出
