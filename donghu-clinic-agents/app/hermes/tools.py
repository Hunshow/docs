# -*- coding: utf-8 -*-
"""Hermes 工具注册表：智能体可调用的工具全部在此登记，均为只读工具（数据红线）。"""  # 模块职责说明
from typing import Dict, Callable, Any, List, Optional   # 类型标注
from app.services import store                           # 业务数据仓库


def tool_get_appointment(apt_id: str) -> Dict[str, Any]:  # 工具：查预约
    """工具-查询预约记录（只读）。

    入参：apt_id 预约编号。
    出参：预约记录 dict（内部视图，出口由路由层统一脱敏）。
    失败行为：不存在返回 {"error": "not_found"}。
    """
    rec = store.get_appointment(apt_id)                  # 查仓库
    return rec if rec else {"error": "not_found"}        # 统一返回结构

def tool_list_queue(dept: Optional[str] = None) -> List[Dict[str, Any]]:  # 工具：查队列
    """工具-查询叫号队列（只读，来自叫号系统视图）。

    入参：dept 科室过滤（None 全部）。
    出参：队列记录列表。
    失败行为：无（空列表）。
    """
    q = store._load().get("queue", [])                   # 读队列视图（只读快照）
    if dept:                                             # 科室过滤
        return [r for r in q if r.get("dept") == dept]   # 过滤返回
    return list(q)                                       # 全量返回

def tool_get_schedule(doctor: Optional[str] = None) -> List[Dict[str, Any]]:  # 工具：查排班
    """工具-查询医生排班（只读，来自 HIS 视图）。

    入参：doctor 医生名过滤（None 全部）。
    出参：排班记录列表（含 emergency_pause 急诊暂停标志：排班未同步检测点）。
    失败行为：无（空列表）。
    """
    rows = store._load().get("schedules", [])            # 读排班视图（只读快照）
    if doctor:                                           # 医生过滤
        return [r for r in rows if r.get("doctor") == doctor]  # 过滤返回
    return list(rows)                                    # 全量返回

def tool_hourly_history() -> List[Dict[str, Any]]:       # 工具：查历史流量
    """工具-查询历史分时到院人次（只读，流量预判输入）。

    入参：无。出参：小时人次列表。失败行为：无（空列表）。
    """
    return list(store._load().get("hourly_history", []))  # 返回历史快照

def tool_kb_search(query: str) -> Dict[str, Any]:        # 工具：知识检索
    """工具-RAG 知识检索（只读）：制度规则 + 图谱三元组。

    入参：query 查询文本。出参：检索结果字典。失败行为：索引为空返回空引用。
    """
    from app.rag.kb import get_kb                        # 延迟导入避免循环
    return get_kb().retrieve(query)                      # 委托知识库

# 工具注册表：新增工具必须在此登记，并保持"只读"性质——写操作一律走人工确认链路
TOOL_REGISTRY: Dict[str, Callable] = {                   # 工具名 → 函数映射
    "get_appointment": tool_get_appointment,             # 查预约
    "list_queue": tool_list_queue,                       # 查队列
    "get_schedule": tool_get_schedule,                   # 查排班
    "hourly_history": tool_hourly_history,               # 查历史流量
    "kb_search": tool_kb_search,                         # 知识检索
}

def call_tool(name: str, **kwargs: Any) -> Any:          # 统一工具调用入口
    """按名称调用注册表中的工具（只读白名单强制）。

    入参：name 工具名；kwargs 工具参数。
    出参：工具返回值。
    失败行为：未注册工具抛 ValueError——白名单之外的工具一律禁止调用（安全卡点）。
    """
    func = TOOL_REGISTRY.get(name)                       # 查注册表
    if func is None:                                     # 未注册
        raise ValueError(f"未注册的工具: {name}")         # 拒绝调用——防止越权工具被注入
    return func(**kwargs)                                # 调用并返回
