# -*- coding: utf-8 -*-
"""审计留痕模块：质控要求每次建议可重建处理链（业务编号/资料版本/事实引用/人工改动/签核人）。"""  # 模块职责说明
import json                                              # JSONL 序列化
import time                                              # 时间戳生成
from pathlib import Path                                 # 路径处理
from typing import Optional, List, Dict, Any             # 类型导入


def _audit_path() -> Path:                               # 内部工具：定位审计文件
    """计算审计日志文件的绝对路径。

    入参：无。出参：Path 对象（项目根/data/audit/audit.jsonl，路径来自 config.yaml）。
    失败行为：无（路径不存在时由调用方创建父目录）。
    """
    from app.config import get_settings                  # 延迟导入，避免循环依赖
    rel = get_settings().security.get("audit_log_path", "data/audit/audit.jsonl")  # 读取配置路径
    root = Path(__file__).resolve().parent.parent.parent  # 项目根目录（security/ 的上两级）
    return root / rel                                    # 返回绝对路径


def audit(actor: str, role: str, action: str, biz_id: str = "-", resource: str = "-",
          data_version: str = "-", detail: Optional[Dict[str, Any]] = None,
          decision: str = "-") -> bool:                  # 写入一条审计记录
    """追加一条审计留痕记录（JSONL 追加写，永不覆盖历史）。

    入参：actor 操作人账号；role 操作人角色；action 动作类型（如 LOGIN/RECOMMEND/CONFIRM）；
          biz_id 业务编号（预约号等）；resource 资源标识；data_version 资料版本号；
          detail 明细字典（含事实引用/人工改动等）；decision 结论（如 PENDING_HUMAN/HUMAN_CONFIRMED）。
    出参：True 写入成功 / False 写入失败（审计失败不阻断业务主流程，但会打告警）。
    失败行为：磁盘异常时捕获并返回 False——留痕失败必须让上层可感知，这里通过返回值传递。
    """
    entry = {                                            # 组装审计条目：字段口径对齐质控要求
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),  # 本地时间戳（确认时间的依据）
        "actor": actor,                                  # 操作人（不记录口令——审计红线：不记录账号口令）
        "role": role,                                    # 角色：用于权限复核
        "action": action,                                # 动作类型
        "biz_id": biz_id,                                # 业务编号：审计重建链路的主键
        "resource": resource,                            # 资源标识：如 appointment/APT-001
        "data_version": data_version,                    # 资料版本：证明建议基于哪一版数据
        "detail": detail or {},                          # 明细：事实引用、人工改动说明等
        "decision": decision,                            # 结论状态：如 PENDING_HUMAN（待人工审核）
    }
    try:                                                 # 磁盘写入有失败可能，必须兜底
        p = _audit_path()                                # 取审计文件路径
        p.parent.mkdir(parents=True, exist_ok=True)      # 确保目录存在
        with open(p, "a", encoding="utf-8") as f:        # 追加模式打开：历史记录不可被覆盖——保证审计完整性
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")  # 写一行 JSONL
        return True                                      # 成功
    except OSError:                                      # 磁盘/权限异常
        return False                                     # 返回失败，由调用方决定是否告警

def read_audit(biz_id: Optional[str] = None, limit: int = 200) -> List[dict]:  # 读取审计记录
    """按业务编号（可选）读取审计记录，供管理员/质控调阅。

    入参：biz_id 业务编号过滤（None 读全部）；limit 最大返回条数（防大文件拖垮接口）。
    出参：审计条目列表（按时间倒序，最新在前）。
    失败行为：文件不存在返回空列表；单行损坏则跳过该行，不中断整体读取。
    """
    p = _audit_path()                                    # 取审计文件路径
    if not p.exists():                                   # 尚无审计文件
        return []                                        # 返回空列表
    rows: List[dict] = []                                # 结果容器
    with open(p, "r", encoding="utf-8") as f:            # 读取整个 JSONL
        for line in f:                                   # 逐行解析（单行损坏不影响其余行）
            line = line.strip()                          # 去除行尾换行
            if not line:                                 # 跳过空行
                continue                                 # 继续下一行
            try:                                         # 容错解析
                obj = json.loads(line)                   # 解析 JSON 行
            except json.JSONDecodeError:                 # 损坏行处理
                continue                                 # 跳过损坏行，保证其余记录可读
            if biz_id and obj.get("biz_id") != biz_id:   # 业务编号过滤
                continue                                 # 不匹配则跳过
            rows.append(obj)                             # 收集命中记录
    return list(reversed(rows))[:limit]                  # 倒序返回最新记录在前
