# -*- coding: utf-8 -*-
"""业务数据仓库：模拟预约平台/HIS/叫号系统的统一只读视图与预约写入，含资料版本管理。"""  # 模块职责说明
import json                                              # 模拟数据序列化
import threading                                         # 线程锁：内存数据并发安全
import time                                              # 版本时间戳
from pathlib import Path                                 # 路径处理
from typing import Dict, List, Optional, Any             # 类型标注
from app.config import get_settings                      # 统一配置

_LOCK = threading.RLock()                                # 可重入锁：创建预约持锁时会调用 persist 再次加锁，必须用 RLock 防死锁
_DATA: Optional[Dict[str, Any]] = None                   # 内存数据缓存：进程生命周期内复用

# 模拟三套外部系统的数据分界：号源+预约来自"预约平台"，医生排班来自"HIS"，签到叫号来自"叫号系统"  # 口径说明注释
_SEED: Dict[str, Any] = {                                # 内置种子数据（首次启动写入 mock 文件）
    "data_version": "V2026.11.12-1",                     # 资料版本号：审计要求建议须绑定数据版本
    "appointments": [                                    # 预约记录列表（预约平台视图）
        {"apt_id": "APT-001", "patient_name": "张三", "phone": "13812345678",  # 患者基本信息（对外须脱敏）
         "medical_no": "MZ20261101001", "dept": "全科门诊", "doctor": "王济民",  # 科室与医生
         "schedule_time": "2026-11-12 09:00", "status": "booked",        # 排班时段与状态
         "arrived": False, "checked_in": False, "source": "online",      # 到院/签到标志与来源
         "owner": "patient01"},                                          # 归属账号：患者本人关联预约用
        {"apt_id": "APT-002", "patient_name": "李四", "phone": "13987654321",
         "medical_no": "MZ20261101002", "dept": "外科门诊", "doctor": "孙明华",  # 多医生样本：避免演示数据清一色
         "schedule_time": "2026-11-12 09:15", "status": "booked",
         "arrived": True, "checked_in": True, "source": "online",
         "owner": None},                                                 # 无归属账号：其他患者的样本
        {"apt_id": "APT-003", "patient_name": "王五", "phone": "13711112222",
         "medical_no": "MZ20261101003", "dept": "全科门诊", "doctor": "王济民",
         "schedule_time": "2026-11-12 09:30", "status": "late_arrival",  # 迟到场景：混队解释负担来源
         "arrived": True, "checked_in": True, "source": "walk_in",       # 现场加号来源
         "owner": None},                                                 # 现场加号：无线上账号归属
        {"apt_id": "APT-004", "patient_name": "赵六", "phone": "13633334444",
         "medical_no": "MZ20261101004", "dept": "内科门诊", "doctor": "钱有光",  # 多医生样本：内科门诊
         "schedule_time": "2026-11-12 09:45", "status": "no_show",       # 爽约场景样本
         "arrived": False, "checked_in": False, "source": "online",
         "owner": None},                                                 # 无归属账号
    ],
    "schedules": [                                       # 医生排班列表（HIS 视图，只读）
        {"doctor": "王济民", "dept": "全科门诊", "date": "2026-11-12",     # 排班日期
         "slot_start": "09:00", "slot_end": "12:00", "capacity": 20,     # 时段与容量
         "status": "active", "emergency_pause": False},                  # 状态与急诊暂停标志（排班未同步检测点）
        {"doctor": "钱有光", "dept": "内科门诊", "date": "2026-11-12",
         "slot_start": "09:00", "slot_end": "12:00", "capacity": 18,
         "status": "active", "emergency_pause": True},                   # 急诊暂停=True：典型的"排班未同步"样本
        {"doctor": "孙明华", "dept": "外科门诊", "date": "2026-11-12",   # 第三位医生：支撑多科室多医生演示
         "slot_start": "09:00", "slot_end": "12:00", "capacity": 15,
         "status": "active", "emergency_pause": False},                  # 正常排班
    ],
    "queue": [                                           # 叫号队列列表（叫号系统视图，只读）
        {"apt_id": "APT-002", "dept": "全科门诊", "position": 1,         # 队列序号
         "check_in_time": "2026-11-12 08:52", "called": False},          # 签到时间与叫号标志
        {"apt_id": "APT-003", "dept": "全科门诊", "position": 2,
         "check_in_time": "2026-11-12 09:41", "called": False},          # 迟到签到样本
    ],
    "hourly_history": [                                  # 历史分时到院人次（流量预判的输入）
        {"hour": 8,  "count": 120}, {"hour": 9,  "count": 260},          # 历史均值样本
        {"hour": 10, "count": 310}, {"hour": 11, "count": 240},          # 高峰在 10 点
        {"hour": 13, "count": 150}, {"hour": 14, "count": 180},
        {"hour": 15, "count": 160}, {"hour": 16, "count": 90},
    ],
}


def _data_file() -> Path:                                # 内部工具：定位模拟数据文件
    """计算模拟数据文件绝对路径。入参无；出参 Path；失败行为无。"""
    rel = get_settings().mock_data.get("appointments_file", "data/mock_appointments.json")  # 读配置路径
    return Path(__file__).resolve().parent.parent.parent / rel  # 项目根 + 相对路径


def _load() -> Dict[str, Any]:                           # 内部工具：加载/初始化数据
    """加载模拟数据：文件存在则读文件，否则写入种子数据。

    入参：无。出参：数据字典。
    失败行为：文件损坏时回退种子数据并保留损坏文件为 .bak，保证服务可启动。
    """
    global _DATA                                         # 使用进程级缓存
    if _DATA is not None:                                # 已加载过
        return _DATA                                     # 直接返回缓存
    p = _data_file()                                     # 取文件路径
    if p.exists():                                       # 文件已存在
        try:                                             # 容错读取
            with open(p, "r", encoding="utf-8") as f:    # UTF-8 读取
                _DATA = json.load(f)                     # 解析
                return _DATA                             # 返回
        except (json.JSONDecodeError, OSError):          # 损坏处理
            p.rename(p.with_suffix(".json.bak"))         # 保留损坏文件备查，不静默删除
    _DATA = json.loads(json.dumps(_SEED, ensure_ascii=False))  # 深拷贝种子数据，防止污染常量
    try:                                                 # 尽力持久化种子
        p.parent.mkdir(parents=True, exist_ok=True)      # 确保目录
        with open(p, "w", encoding="utf-8") as f:        # 写文件
            json.dump(_DATA, f, ensure_ascii=False, indent=2)  # 序列化写入
    except OSError:                                      # 写失败不阻断（内存数据仍可用）
        pass                                             # 忽略持久化失败
    return _DATA                                         # 返回数据


def bump_version() -> str:                               # 资料版本推进
    """业务数据发生变更时推进资料版本号，供审计绑定。

    入参：无。出参：新版本号字符串。
    失败行为：无（内存操作）。
    """
    d = _load()                                          # 取数据
    seq = d.get("data_version", "V2026.11.12-0").rsplit("-", 1)  # 拆出序号部分
    try:                                                 # 序号自增容错
        n = int(seq[1]) + 1                              # 序号 +1
    except (ValueError, IndexError):                     # 非标准版本号
        n = 1                                            # 重置为 1
    new_ver = f"{seq[0]}-{n}"                            # 组装新版本号
    d["data_version"] = new_ver                          # 回写
    return new_ver                                       # 返回新版本


def persist() -> bool:                                   # 持久化内存数据
    """将内存数据写回模拟数据文件。

    入参：无。出参：True 成功 / False 失败。
    失败行为：磁盘异常返回 False，调用方留痕告警。
    """
    with _LOCK:                                          # 加锁防止并发写坏文件
        d = _load()                                      # 取最新数据
        try:                                             # 磁盘容错
            p = _data_file()                             # 取路径
            p.parent.mkdir(parents=True, exist_ok=True)  # 确保目录
            tmp = p.with_suffix(".json.tmp")             # 先写临时文件
            with open(tmp, "w", encoding="utf-8") as f:  # 写临时
                json.dump(d, f, ensure_ascii=False, indent=2)  # 序列化
            tmp.replace(p)                               # 原子替换，防止写一半损坏——数据完整性考虑
            return True                                  # 成功
        except OSError:                                  # 磁盘异常
            return False                                 # 失败返回


def get_data_version() -> str:                           # 读取当前资料版本
    """获取当前资料版本号。入参无；出参版本字符串；失败行为无。"""
    return _load().get("data_version", "-")              # 返回版本


def list_appointments(dept: Optional[str] = None) -> List[dict]:  # 预约列表
    """按科室（可选）列出全部预约记录（内部视图，含敏感字段，出口必须脱敏）。

    入参：dept 科室过滤（None 全部）。出参：预约记录列表。
    失败行为：无。
    """
    rows = _load().get("appointments", [])               # 取预约列表
    if dept:                                             # 科室过滤
        return [r for r in rows if r.get("dept") == dept]  # 过滤返回
    return list(rows)                                    # 全量返回

def get_appointment(apt_id: str) -> Optional[dict]:      # 单条预约
    """按预约号查询单条预约。

    入参：apt_id 预约编号。出参：预约记录或 None。
    失败行为：不存在返回 None。
    """
    for r in _load().get("appointments", []):            # 线性查找（演示规模足够）
        if r.get("apt_id") == apt_id:                    # 编号匹配
            return r                                     # 返回命中记录
    return None                                          # 未找到

def create_appointment(payload: dict, owner: Optional[str] = None) -> dict:  # 新建预约（患者动作）
    """创建新预约：校验必填、生成编号、写入存储并推进版本。

    入参：payload 含 patient_name/phone/medical_no/dept/doctor/schedule_time；
         owner 预约发起账号（用于"我的预约"按账号关联，可不传）。
    出参：新建的预约记录（含生成的 apt_id 与资料版本）。
    失败行为：缺少必填字段抛 ValueError；号源校验由预约核验智能体负责，这里只管落库。
    """
    required = ["patient_name", "phone", "medical_no", "dept", "doctor", "schedule_time"]  # 必填字段清单
    missing = [k for k in required if not payload.get(k)]  # 找缺失字段
    if missing:                                          # 有缺失
        raise ValueError(f"缺少必填字段: {','.join(missing)}")  # 拒绝创建——入口数据完整性卡点
    with _LOCK:                                          # 加锁保证编号生成唯一
        d = _load()                                      # 取数据
        seq = len(d.get("appointments", [])) + 1         # 以条数生成序号（演示规模可行）
        apt = {                                          # 组装预约记录
            "apt_id": f"APT-{seq:03d}",                  # 生成业务编号：审计链路主键
            "patient_name": payload["patient_name"],     # 患者姓名（出口脱敏）
            "phone": payload["phone"],                   # 手机号（出口脱敏）
            "medical_no": payload["medical_no"],         # 病历号（出口脱敏）
            "dept": payload["dept"],                     # 科室
            "doctor": payload["doctor"],                 # 医生
            "schedule_time": payload["schedule_time"],   # 预约时段
            "status": "booked",                          # 初始状态：已预约
            "arrived": False, "checked_in": False,       # 尚未到院/签到
            "source": "online",                          # 来源：线上预约
            "owner": owner,                              # 归属账号：谁在平台上发起的预约，用于本人关联查询
        }
        d.setdefault("appointments", []).append(apt)     # 追加
        bump_version()                                   # 推进资料版本（审计绑定新版本）
        persist()                                        # 持久化
        return dict(apt)                                 # 返回副本
