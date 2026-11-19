# -*- coding: utf-8 -*-
"""四个业务智能体：预约核验、流量预判、动态分诊推荐、协同提醒。

统一约束：所有输出均为"建议"，必须人工确认后才能生效；不生成诊疗结论。  # 红线说明
"""
from typing import Dict, Any, List, Optional             # 类型标注
from app.hermes.engine import HermesAgent, call_ollama   # 引擎基类与模型调用
from app.hermes import tools                             # 只读工具注册表


class AppointmentVerifierAgent(HermesAgent):             # 预约核验智能体
    """核验预约信息：号源存在性、排班状态、到院签到一致性，异常转人工。"""

    name = "appointment_verifier"                        # 智能体名称
    role = "verifier"                                    # 角色标识
    description = "核验预约与排班一致性，识别排班未同步等异常"  # 职责描述
    domain_keywords = "预约 核验 排班 一致性 签到 转人工"      # 检索扩展词：保证召回制度依据

    def solve(self, task: Dict[str, Any], context: Dict[str, Any], mode: str) -> Dict[str, Any]:  # 业务求解
        """执行预约核验。

        入参：task 需含 apt_id；context 感知上下文；mode 推理模式。
        出参：{"summary","findings","requires_human_confirm","escalation","tool_calls"}。
        失败行为：预约不存在时返回 requires_human_confirm=True（转人工核实）。
        """
        apt_id = str(task.get("apt_id", ""))             # 取预约编号
        calls = [{"tool": "get_appointment", "args": {"apt_id": apt_id}}]  # 记录工具调用（审计用）
        apt = tools.call_tool("get_appointment", apt_id=apt_id)  # 查预约
        if apt.get("error"):                             # 预约不存在
            return {"summary": f"预约 {apt_id} 不存在，转人工核实",  # 明确转人工
                    "findings": [{"check": "存在性", "result": "未找到该预约号"}],  # 核验发现
                    "requires_human_confirm": True,      # 人工确认
                    "escalation": "预约号不存在，请人工核对是否为录入错误",  # 升级说明
                    "tool_calls": calls}                 # 工具调用记录
        findings: List[Dict[str, str]] = []              # 核验发现列表
        scheds = tools.call_tool("get_schedule", doctor=apt.get("doctor"))  # 查医生排班
        calls.append({"tool": "get_schedule", "args": {"doctor": apt.get("doctor")}})  # 记录调用
        sched = scheds[0] if scheds else None            # 取首个排班（演示数据单班次）
        if sched is None:                                # 无排班记录
            findings.append({"check": "排班一致性", "result": "医生无排班记录，属资料缺少依据场景"})  # 兜底场景二
            return {"summary": f"预约 {apt_id} 依赖的排班缺失，转人工",  # 转人工
                    "findings": findings, "requires_human_confirm": True,  # 人工确认
                    "escalation": "资料缺少依据，转排班管理员核对",       # 升级：场景二
                    "tool_calls": calls}                 # 调用记录
        if sched.get("emergency_pause"):                 # 医生急诊暂停（排班未同步典型场景）
            findings.append({"check": "排班状态", "result": "医生临时处理急诊，排班未同步，原准点到诊通知不可信"})  # 场景一
            return {"summary": f"预约 {apt_id} 命中'排班未同步'兜底场景，暂停自动建议，转人工",  # 摘要
                    "findings": findings,                # 发现
                    "requires_human_confirm": True,      # 必须人工确认
                    "escalation": "排班未同步：转排班管理员核对（触发原因=急诊暂停/接手人=排班管理员/确认时间=人工签收时刻）",  # 三要素
                    "tool_calls": calls}                 # 调用记录
        findings.append({"check": "排班一致性", "result": "排班有效，时段匹配"})  # 正常项
        if apt.get("arrived") and not apt.get("checked_in"):  # 到院未签到：口径差异点
            findings.append({"check": "到院/签到一致性", "result": "患者已到院但未签到，等待计时起点存在口径差异"})  # 口径提示
        findings.append({"check": "号源有效性", "result": f"状态={apt.get('status')}，来源={apt.get('source')}"})  # 状态项
        return {"summary": f"预约 {apt_id} 核验通过，未触发兜底场景",  # 摘要
                "findings": findings,                    # 发现
                "requires_human_confirm": False,         # 核验属事实比对，无临床判断，无需人工终审
                "escalation": "",                        # 无升级
                "tool_calls": calls}                     # 调用记录


class FlowForecasterAgent(HermesAgent):                  # 流量预判智能体
    """基于历史分时人次与当前队列，预判候诊压力并给出流量预警（只提示，不调度）。"""

    name = "flow_forecaster"                             # 名称
    role = "forecaster"                                  # 角色
    description = "预判候诊压力，输出流量预警提示"          # 职责
    domain_keywords = "流量 预判 高峰 候诊 预警 增开"        # 检索扩展词

    def solve(self, task: Dict[str, Any], context: Dict[str, Any], mode: str) -> Dict[str, Any]:  # 求解
        """执行流量预判。

        入参：task 可含 current_hour（缺省用当前队列规模推断）；context 上下文；mode 模式。
        出参：{"summary","peak_hour","expected_count","queue_size","warning","requires_human_confirm","tool_calls"}。
        失败行为：历史数据为空时返回"数据不足，转人工"。
        """
        calls = [{"tool": "hourly_history", "args": {}},  # 记录工具调用
                 {"tool": "list_queue", "args": {}}]     # 记录调用
        history = tools.call_tool("hourly_history")      # 读历史
        queue = tools.call_tool("list_queue")            # 读队列
        if not history:                                  # 无历史数据
            return {"summary": "历史流量数据不足，无法预判，转人工",  # 数据不足兜底
                    "warning": "数据缺少依据", "requires_human_confirm": True,  # 转人工（场景二）
                    "escalation": "历史数据缺失，流量预判不可用", "tool_calls": calls}  # 升级
        peak = max(history, key=lambda h: int(h.get("count", 0)))  # 找历史高峰时段
        peak_hour = int(task.get("current_hour", peak["hour"]))    # 目标时段（缺省用高峰）
        expected = next((int(h["count"]) for h in history if int(h["hour"]) == peak_hour),  # 目标时段人次
                        int(peak["count"]))              # 找不到则用高峰值
        queue_size = len(queue)                          # 当前排队规模
        warning = ""                                     # 预警文本
        if expected >= 300 or queue_size >= 10:          # 阈值预警（演示阈值，试点期由管理员配置）
            warning = "高峰预警：预计人流/队列超出常态，建议增开叫号口并通知导诊加强解释"  # 预警内容
        summary = (f"预判 {peak_hour} 时段到院约 {expected} 人次，"       # 摘要组装
                   f"当前在队 {queue_size} 人；历史高峰 {peak['hour']} 时（{peak['count']} 人次）。")
        if mode == "llm":                                # 模型可用时让本地模型润色解释文本
            narr = call_ollama(f"用一句话向分诊护士解释以下流量预判结果：{summary}")  # 本地生成
            if narr:                                     # 生成成功
                summary = f"{summary} 解释：{narr}"      # 追加模型解释
        return {"summary": summary,                      # 摘要
                "peak_hour": peak_hour,                  # 预判时段
                "expected_count": expected,              # 预计人次
                "queue_size": queue_size,                # 当前队列
                "warning": warning,                      # 预警
                "requires_human_confirm": bool(warning), # 有预警才需人工确认调度——预判本身是事实陈述
                "escalation": "",                        # 无升级
                "tool_calls": calls}                     # 调用记录


class TriageAdvisorAgent(HermesAgent):                   # 动态分诊推荐智能体
    """产出分诊推荐（次序解释/优先级建议），一律 PENDING_HUMAN，不落终审、不改队列。"""

    name = "triage_advisor"                              # 名称
    role = "advisor"                                     # 角色
    description = "产出待人工确认的分诊推荐，绝不直接改队列或下结论"  # 职责
    domain_keywords = "分诊 推荐 次序 队列 人工确认 迟到 加号"  # 检索扩展词

    def solve(self, task: Dict[str, Any], context: Dict[str, Any], mode: str) -> Dict[str, Any]:  # 求解
        """生成分诊推荐。

        入参：task 需含 apt_id；context 上下文；mode 模式。
        出参：{"summary","recommendation","recommend_id","requires_human_confirm"(恒 True),"tool_calls"}。
        失败行为：目标预约不存在或命中兜底场景时转人工。
        """
        apt_id = str(task.get("apt_id", ""))             # 目标预约
        calls = [{"tool": "get_appointment", "args": {"apt_id": apt_id}},  # 调用记录
                 {"tool": "list_queue", "args": {}},     # 调用记录
                 {"tool": "get_schedule", "args": {}}]   # 调用记录
        apt = tools.call_tool("get_appointment", apt_id=apt_id)  # 查预约
        if apt.get("error"):                             # 不存在
            return {"summary": f"预约 {apt_id} 不存在，无法生成推荐", "recommendation": None,  # 拒绝
                    "requires_human_confirm": True, "escalation": "预约号异常，转人工核实", "tool_calls": calls}  # 转人工
        scheds = tools.call_tool("get_schedule", doctor=apt.get("doctor"))  # 查排班
        if scheds and scheds[0].get("emergency_pause"):  # 排班未同步场景
            return {"summary": f"预约 {apt_id} 所涉医生急诊暂停，禁止自动推荐次序",  # 场景一硬卡点
                    "recommendation": None,              # 不给推荐——错误建议比没有建议更危险
                    "requires_human_confirm": True,      # 转人工
                    "escalation": "排班未同步：触发原因=医生急诊暂停；接手人=排班管理员；确认时间=人工签收时刻",  # 三要素
                    "tool_calls": calls}                 # 调用记录
        queue = tools.call_tool("list_queue")            # 读队列
        # 推荐逻辑：按签到时间排序解释次序（只解释，不改队列）；迟到/加号单独标注
        ordered = sorted(queue, key=lambda r: str(r.get("check_in_time", "")))  # 签到时间升序=建议叫号次序
        position = next((i + 1 for i, r in enumerate(ordered) if r.get("apt_id") == apt_id), None)  # 目标位次
        rec_text = ""                                    # 推荐文本
        if position:                                     # 在队内
            rec_text = f"建议按签到时间次序叫号，{apt_id} 当前为第 {position} 位"  # 次序解释
        else:                                            # 不在队（未签到/爽约/加号）
            status = apt.get("status", "-")              # 取状态
            rec_text = f"{apt_id} 尚未进入叫号队列（状态={status}），建议导诊按迟到/加号规则引导签到"  # 引导建议
        if apt.get("status") == "late_arrival" or apt.get("source") == "walk_in":  # 迟到/加号混队
            rec_text += "；该患者属迟到/现场加号，混队解释须显示原记录与待确认字段"  # 引用一线要求
        return {"summary": rec_text,                     # 摘要
                "recommendation": {"apt_id": apt_id,     # 结构化推荐
                                   "suggest_order_position": position,  # 建议位次
                                   "text": rec_text},   # 文本
                "recommend_id": f"REC-{apt_id}",         # 推荐编号：人工确认链路主键
                "requires_human_confirm": True,          # 恒为 True：分诊推荐必须人工终审——数据红线
                "escalation": "",                        # 无升级
                "tool_calls": calls}                     # 调用记录


class CollabNotifierAgent(HermesAgent):                  # 协同提醒智能体
    """生成面向患者/医护的提醒与升级通知；涉及对患者的对外承诺一律转人工发送。"""

    name = "collab_notifier"                             # 名称
    role = "notifier"                                    # 角色
    description = "生成协同提醒与异常升级通知，不自动对外承诺"  # 职责
    domain_keywords = "协同 提醒 转人工 通知 三要素 接手人"     # 检索扩展词

    def solve(self, task: Dict[str, Any], context: Dict[str, Any], mode: str) -> Dict[str, Any]:  # 求解
        """生成提醒/升级通知。

        入参：task 需含 notify_type（patient_reminder|staff_escalation）与 apt_id；context；mode。
        出参：{"summary","message","audience","requires_human_confirm","escalation","tool_calls"}。
        失败行为：类型未知时返回错误提示并转人工。
        """
        ntype = str(task.get("notify_type", ""))         # 通知类型
        apt_id = str(task.get("apt_id", "-"))            # 关联预约
        calls = [{"tool": "get_appointment", "args": {"apt_id": apt_id}}]  # 调用记录
        if ntype == "patient_reminder":                  # 患者提醒
            msg = f"尊敬的患者：您的预约 {apt_id} 即将到号，请前往候诊区留意叫号，超一个更新周期的信息不作实时承诺。"  # 文案含口径声明
            return {"summary": "生成患者候诊提醒", "message": msg, "audience": "patient",  # 返回
                    "requires_human_confirm": True,      # 对患者的通知必须医护确认后发出——防错误承诺
                    "escalation": "", "tool_calls": calls}  # 无升级
        if ntype == "staff_escalation":                  # 医护升级
            msg = (f"【转人工】预约 {apt_id} 触发兜底场景：{task.get('reason', '排班未同步')}。"  # 触发原因
                   f"接手人：{task.get('receiver', '排班管理员')}；"  # 接手人
                   f"确认时间：以人工签收时刻为准。")     # 确认时间口径
            return {"summary": "生成医护升级通知（含三要素）", "message": msg, "audience": "staff",  # 返回
                    "requires_human_confirm": True,      # 升级必须人工接手确认
                    "escalation": str(task.get("reason", "排班未同步")), "tool_calls": calls}  # 升级原因
        return {"summary": f"未知通知类型: {ntype}", "message": None,  # 未知类型
                "audience": None, "requires_human_confirm": True,  # 转人工
                "escalation": "通知类型配置错误，请管理员检查", "tool_calls": calls}  # 升级


# 智能体注册表：按 config.yaml 的 hermes.agents 清单装配（配置驱动，不写死实例化顺序）
_AGENT_CLASSES = {                                       # 角色名 → 类映射
    "verifier": AppointmentVerifierAgent,                # 预约核验
    "forecaster": FlowForecasterAgent,                   # 流量预判
    "advisor": TriageAdvisorAgent,                       # 分诊推荐
    "notifier": CollabNotifierAgent,                     # 协同提醒
}

def get_agents() -> Dict[str, HermesAgent]:              # 装配智能体实例
    """按配置装配全部智能体实例。

    入参：无。出参：{agent_name: 实例} 字典。
    失败行为：配置引用了未实现的角色时抛 ValueError，阻止带病启动。
    """
    from app.config import get_settings                  # 延迟导入
    out: Dict[str, HermesAgent] = {}                     # 结果容器
    for item in get_settings().hermes.get("agents", []):  # 遍历配置清单
        role = str(item.get("role", ""))                 # 角色
        cls = _AGENT_CLASSES.get(role)                   # 查类
        if cls is None:                                  # 未实现的角色
            raise ValueError(f"未实现的智能体角色: {role}")  # 启动即失败——配置错误要尽早暴露
        out[str(item.get("name", role))] = cls()         # 实例化
    return out                                           # 返回
