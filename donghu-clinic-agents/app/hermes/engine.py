# -*- coding: utf-8 -*-
"""Hermes 引擎：智能体基类 + 感知-思考-行动循环 + Ollama 本地模型调用与规则降级。"""  # 模块职责说明
import httpx                                             # 调用本地 Ollama HTTP API
from typing import Dict, List, Optional, Any             # 类型标注
from app.config import get_settings                      # 统一配置
from app.rag.kb import get_kb                            # RAG 知识库单例


def call_ollama(prompt: str) -> Optional[str]:           # Ollama 本地模型调用
    """调用本地 Ollama 生成文本；不可用时返回 None（由调用方降级为规则推理）。

    入参：prompt 提示词文本。
    出参：模型输出字符串；连接失败/超时/非 200 返回 None。
    失败行为：任何异常捕获后返回 None，绝不抛出——模型故障不能拖垮业务主流程。
    """
    cfg = get_settings().ollama                          # 读取 Ollama 配置
    try:                                                 # 网络调用全程容错
        resp = httpx.post(                               # POST 生成接口
            f"{cfg.get('base_url')}/api/generate",       # 本地地址，来自 config.yaml（禁止写死代码）
            json={"model": cfg.get("chat_model", ""),    # 模型名来自配置
                  "prompt": prompt, "stream": False,     # 关闭流式：简化同步调用
                  "options": {"temperature": 0.2}},      # 低温度：业务建议要稳定可复现
            timeout=float(cfg.get("timeout_seconds", 10)),  # 超时来自配置
        )
        if resp.status_code == 200:                      # 成功响应
            return str(resp.json().get("response", "")).strip() or None  # 返回非空文本
        return None                                      # 非 200 视为不可用
    except (httpx.HTTPError, ValueError, KeyError):      # 网络/解析异常
        return None                                      # 降级信号：返回 None


class HermesAgent:                                       # 智能体基类
    """Hermes 智能体基类：统一执行 感知(perceive)→思考(think)→行动(act) 循环。

    子类只需实现 solve(task, context) 的业务逻辑；基类负责：
    RAG 检索注入、步数控制、Ollama 可用性探测、审计所需的上下文准备。
    所有智能体的输出都只是"建议"，最终决定权在人工——这是不可逾越的产品边界。
    """

    name: str = "base"                                   # 智能体名称（子类覆盖）
    role: str = "assistant"                              # 智能体角色（子类覆盖）
    description: str = ""                                # 职责描述（子类覆盖）
    domain_keywords: str = ""                            # 领域关键词：检索查询扩展用（子类覆盖）

    def __init__(self) -> None:                          # 构造
        """初始化智能体：挂载知识库，记录最大步数。

        入参：无。出参：实例。
        失败行为：知识库构建失败时降级为空索引（检索为空但不抛异常）。
        """
        self.kb = get_kb()                               # 挂载 RAG 知识库（含知识图谱）
        self.max_steps = int(get_settings().hermes.get("max_steps", 4))  # 步数上限，防循环失控

    def perceive(self, task: Dict[str, Any]) -> Dict[str, Any]:  # 感知阶段
        """感知阶段：解析任务并用 RAG 检索相关制度规则与图谱三元组。

        入参：task 任务字典（含 query 及各智能体所需参数）。
        出参：上下文字典 {"query","references","graph_triples","graph_entities"}。
        失败行为：检索为空时返回空引用列表，不阻断思考阶段。
        """
        query = str(task.get("query", "")).strip()       # 取自然语言查询
        # 查询扩展：用户短查询（如"核验"）在 BM25 下常命不中制度文档，
        # 拼上本智能体的领域关键词可稳定召回，且不会改变业务判定——只影响"引用哪几条依据"
        if self.domain_keywords:                         # 子类声明了领域词才扩展
            query = f"{query} {self.domain_keywords}".strip()  # 拼接扩展词
        result = self.kb.retrieve(query)                 # RAG 检索（BM25 + 图谱扩展）
        return {"query": query, **result}                # 合并上下文

    def think(self, context: Dict[str, Any]) -> str:     # 思考阶段
        """思考阶段：探测 Ollama 可用性，决定本轮用模型推理还是规则推理。

        入参：context 感知上下文。
        出参：模式字符串 "llm"（模型可用）或 "rules"（降级规则推理）。
        失败行为：Ollama 不可用自动降级，不抛异常——可用性优先。
        """
        cfg = get_settings().ollama                      # 读配置
        if not cfg.get("fallback_to_rules", True):       # 明确关闭降级开关
            return "llm"                                 # 强制模型模式
        probe = call_ollama("ping")                      # 轻量探测模型可用性
        return "llm" if probe is not None else "rules"   # 可用则模型模式，否则规则模式

    def act(self, task: Dict[str, Any], context: Dict[str, Any], mode: str) -> Dict[str, Any]:  # 行动阶段
        """行动阶段：调用子类 solve 产出业务建议。

        入参：task 任务；context 上下文；mode 推理模式。
        出参：子类 solve 的结果字典。
        失败行为：solve 抛异常时捕获并返回统一失败结果，不让单次任务击穿服务。
        """
        try:                                             # 业务逻辑容错
            return self.solve(task, context, mode)       # 委托子类实现
        except Exception as e:                           # 任何业务异常
            return {"summary": f"智能体执行失败: {e}",    # 统一失败结果
                    "requires_human_confirm": True,      # 失败结果一律转人工——兜底原则
                    "escalation": "执行异常，转人工处理",  # 升级说明
                    "tool_calls": []}                    # 无工具调用

    def run(self, task: Dict[str, Any]) -> Dict[str, Any]:  # 主循环入口
        """执行一次完整任务：感知→思考→行动，步数受 max_steps 约束。

        入参：task 任务字典。
        出参：{"agent","role","mode","summary","references","graph_triples",
              "requires_human_confirm","escalation","tool_calls","data_version"}。
        失败行为：各阶段异常统一收敛为"转人工"结果——系统宁可罢手也不能给错误建议。
        """
        context: Dict[str, Any] = {}                     # 上下文容器
        mode = "rules"                                   # 推理模式默认值：降级规则推理（安全默认）
        for step in range(self.max_steps):               # Hermes 循环：最多 max_steps 步
            if step == 0:                                # 第一步：感知
                context = self.perceive(task)            # RAG 检索注入
                continue                                 # 进入下一步
            if step == 1:                                # 第二步：思考
                mode = self.think(context)               # 决定推理模式
                continue                                 # 进入行动步
            break                                        # 业务智能体固定三步，其余步数预留扩展
        result = self.act(task, context, mode)           # 行动：产出建议
        from app.services.store import get_data_version  # 延迟导入数据仓库
        result.setdefault("agent", self.name)            # 补充智能体标识
        result.setdefault("role", self.role)             # 补充角色
        result["mode"] = mode                            # 记录推理模式（审计可追溯用了哪种推理）
        result["references"] = context.get("references", [])  # 事实引用：审计要素
        result["graph_triples"] = context.get("graph_triples", [])  # 图谱三元组：规则依据
        result.setdefault("requires_human_confirm", True)  # 默认所有建议都需人工确认——红线
        result["data_version"] = get_data_version()      # 绑定资料版本：审计要素
        return result                                    # 返回

    def solve(self, task: Dict[str, Any], context: Dict[str, Any], mode: str) -> Dict[str, Any]:  # 子类钩子
        """业务求解钩子：子类必须覆盖。

        入参：task 任务；context 上下文；mode 推理模式。
        出参：建议结果字典。
        失败行为：由基类 act 捕获并转人工。
        """
        raise NotImplementedError("子类必须实现 solve")   # 抽象约束
