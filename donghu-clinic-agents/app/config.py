# -*- coding: utf-8 -*-
"""统一配置加载模块：全项目唯一的 config.yaml 读取入口，禁止其他模块直接读配置文件。"""  # 模块职责说明
import yaml                                             # 引入 YAML 解析库（依赖清单内）
from pathlib import Path                                # 引入路径处理，兼容 Windows/Linux
from functools import lru_cache                         # 引入缓存装饰器，避免重复读盘


class Settings:                                          # 配置对象：集中持有全部配置段
    """全局配置对象。

    入参：无（自动从项目根目录 config.yaml 读取）。
    出参：实例属性 app/security/ollama/rag/hermes/demo_users/mock_data 均为 dict/list。
    失败行为：配置文件缺失或格式非法时抛出 RuntimeError，阻止服务带病启动。
    """

    def __init__(self) -> None:                          # 构造函数：加载配置
        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"  # 定位项目根目录下的 config.yaml
        if not cfg_path.exists():                        # 校验配置文件存在
            raise RuntimeError(f"配置文件不存在: {cfg_path}")  # 缺配置直接失败，不使用任何默认密钥
        with open(cfg_path, "r", encoding="utf-8") as f:  # 以 UTF-8 读取，避免中文乱码
            raw = yaml.safe_load(f) or {}                 # 安全解析 YAML（禁止用 load，防任意对象注入——安全考虑）
        self.app: dict = raw.get("app", {})               # 应用基础段
        self.security: dict = raw.get("security", {})     # 安全段：密钥/审计/脱敏
        self.ollama: dict = raw.get("ollama", {})         # Ollama 本地模型段
        self.rag: dict = raw.get("rag", {})               # RAG 知识库段
        self.hermes: dict = raw.get("hermes", {})         # Hermes 智能体段
        self.demo_users: list = raw.get("demo_users", []) # 演示账号段
        self.mock_data: dict = raw.get("mock_data", {})   # 模拟数据段
        if not self.security.get("secret_key"):           # 校验签名密钥必须配置
            raise RuntimeError("security.secret_key 未配置，拒绝启动")  # 无密钥则令牌不可信，必须终止


@lru_cache(maxsize=1)                                    # 进程内缓存：配置只加载一次
def get_settings() -> Settings:                          # 全局获取配置的统一入口
    """获取全局配置单例。

    入参：无。出参：Settings 实例。
    失败行为：底层配置解析失败时向上抛出 RuntimeError。
    """
    return Settings()                                    # 构造并返回配置对象
