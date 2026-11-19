# -*- coding: utf-8 -*-
"""服务入口：创建 FastAPI 应用、挂载路由、注册审计中间件，本地启动。"""  # 模块职责说明
from fastapi import FastAPI, Request                      # 应用与请求对象
from fastapi.responses import RedirectResponse            # 根路径跳转到演示界面
from fastapi.staticfiles import StaticFiles               # 静态资源挂载（前端页面本地加载）
from pathlib import Path                                  # 定位 web 目录
from fastapi.exceptions import RequestValidationError     # 参数校验异常（422 的来源）
from starlette.exceptions import HTTPException as StarletteHTTPException  # HTTP 异常（404 等）
from app.config import get_settings                       # 统一配置
from app.response import ok_response, err_response        # 统一响应
from app.security.audit import audit                      # 审计留痕
from app.api.routes import (router_auth, router_patient, router_staff,  # 业务路由
                            router_agents, router_admin)  # 业务路由（续）
from app.rag.kb import get_kb                             # 知识库（启动时预热索引）


def create_app() -> FastAPI:                              # 应用工厂
    """创建并配置 FastAPI 应用。

    入参：无。出参：配置完成的 FastAPI 实例。
    失败行为：配置非法时 get_settings 抛 RuntimeError，阻止启动。
    """
    settings = get_settings()                             # 读取配置
    app = FastAPI(title=settings.app.get("name", "门诊协作智能体系统"))  # 创建应用
    app.include_router(router_auth)                       # 挂载认证路由
    app.include_router(router_patient)                    # 挂载患者路由
    app.include_router(router_staff)                      # 挂载医护路由
    app.include_router(router_agents)                     # 挂载智能体路由
    app.include_router(router_admin)                      # 挂载管理员路由

    @app.exception_handler(RequestValidationError)        # 统一校验异常处理：422 必须收敛为统一结构——规范硬要求
    async def on_validation_error(request: Request, exc: RequestValidationError):  # 处理校验失败
        """把 FastAPI 参数校验异常转换为统一响应结构。

        入参：request 请求；exc 校验异常。出参：统一错误 JSONResponse（400）。
        失败行为：无。
        """
        return err_response("VALIDATION_ERROR", "请求参数不完整或格式错误", 400)  # 统一错误码，不回传内部字段明细

    @app.exception_handler(StarletteHTTPException)        # 统一 HTTP 异常处理（404 等）
    async def on_http_error(request: Request, exc: StarletteHTTPException):  # 处理 HTTP 异常
        """把框架 HTTP 异常转换为统一响应结构。

        入参：request 请求；exc HTTP 异常。出参：统一错误 JSONResponse。
        失败行为：无。
        """
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"  # 错误码映射
        return err_response(code, str(exc.detail), exc.status_code)  # 统一结构返回

    @app.get("/api/health", tags=["system"])              # 健康检查接口
    def health():                                         # 处理健康检查
        """健康检查：返回服务与知识库状态。

        入参：无。出参：统一结构，data 含状态与分块数。
        失败行为：无。
        """
        kb = get_kb()                                     # 取知识库（惰性构建）
        return ok_response({"status": "UP",               # 服务状态
                            "kb_chunks": len(kb.chunks),  # 知识分块数
                            "graph_triples": len(kb.graph.triples)})  # 图谱三元组数

    # ---------------- MVP 演示界面 ----------------
    web_dir = Path(__file__).resolve().parent / "web"      # 前端静态目录（与 main.py 同级）
    if web_dir.exists():                                  # 目录存在才挂载，避免环境缺失导致启动失败
        app.mount("/ui", StaticFiles(directory=str(web_dir), html=True), name="ui")  # 挂载到 /ui

        @app.get("/", include_in_schema=False)            # 根路径
        def index_redirect():                             # 直接跳转到演示界面
            """根路径重定向到 /ui/，方便演示时只记一个地址。

            入参：无。出参：307 重定向响应。
            失败行为：无（静态目录缺失时该路由不会被注册）。
            """
            return RedirectResponse(url="/ui/")           # 跳到静态首页

    @app.middleware("http")                               # 注册 HTTP 中间件
    async def audit_middleware(request: Request, call_next):  # 请求级审计中间件
        """中间件：记录每个请求的方法与路径（不含任何请求体内容，防敏感信息入审计）。

        入参：request 请求；call_next 下游处理。出参：响应。
        失败行为：审计失败不影响请求处理——留痕尽力而为，业务优先。
        """
        response = await call_next(request)               # 先放行业务处理
        audit(actor="system", role="middleware",          # 系统级留痕
              action="HTTP",                              # 动作类型
              resource=f"{request.method} {request.url.path}",  # 方法+路径（不含 query/body，避免敏感信息入档）
              decision=f"HTTP_{response.status_code}")    # 记录响应码
        return response                                   # 返回响应

    return app                                            # 返回应用

app = create_app()                                        # 模块级应用实例（uvicorn 入口）

if __name__ == "__main__":                                # 直接运行入口
    import os                                             # 读取平台注入的环境变量 PORT
    import uvicorn                                        # 引入 ASGI 服务器
    s = get_settings()                                    # 读配置
    # 关键逻辑：托管平台只注入 PORT，且必须绑定 0.0.0.0 才能被反向代理访问；本地运行则坚持回环地址
    # 之所以按“有无 PORT 环境变量”分流，是为了不破坏“数据本地保存、不对外暴露”的红线：
    # 只有部署方（平台）显式注入 PORT 时才打开对外监听，本机双击运行时绝不会意外暴露到局域网
    env_port = os.environ.get("PORT")                     # 平台注入的端口（本地运行时为空）
    if env_port:                                          # 托管环境：必须对外监听
        host, port = "0.0.0.0", int(env_port)             # 绑定全部网卡 + 平台指定端口
    else:                                                 # 本地环境：保持仅本机可访问
        host = s.app.get("host", "127.0.0.1")             # 监听地址来自配置（本地回环）
        port = int(s.app.get("port", 8310))               # 端口来自配置
    uvicorn.run(app, host=host, port=port)                # 启动服务（配置驱动，代码零硬编码地址）
