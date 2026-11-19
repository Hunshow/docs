# -*- coding: utf-8 -*-
"""统一响应结构模块：所有对外接口必须经此封装，保证 {"ok", "data", "error"} 结构一致。"""  # 模块职责说明
from fastapi.responses import JSONResponse              # 引入 JSON 响应类，用于带 HTTP 状态码的错误返回
from typing import Any, Optional                        # 类型标注


def ok_response(data: Any = None) -> dict:               # 成功响应封装
    """构造成功响应体。

    入参：data 为任意业务数据（默认 None）。
    出参：{"ok": True, "data": data, "error": None}。
    失败行为：无（纯构造函数，不产生副作用）。
    """
    return {"ok": True, "data": data, "error": None}     # 成功体：error 恒为 None，便于调用方统一判断


def ok_json(data: Any = None) -> JSONResponse:           # 带响应头的成功响应封装
    """构造成功响应对象（需要写响应头或 Cookie 时使用）。

    入参：data 为任意业务数据（默认 None）。
    出参：JSONResponse，body 为统一成功结构 {"ok": True, "data": data, "error": None}。
    失败行为：无。说明：普通接口直接用 ok_response 返回 dict 即可，只有必须设置响应头
    （例如登录下发 HttpOnly Cookie）时才用本函数，保证结构仍由统一模块收口。
    """
    return JSONResponse(status_code=200, content=ok_response(data))  # 体结构与 dict 版完全一致


def err_response(code: str, msg: str, http_status: int = 400) -> JSONResponse:  # 失败响应封装
    """构造失败响应体。

    入参：code 为业务错误码（如 AUTH_FAILED），msg 为中文错误说明，http_status 为 HTTP 状态码。
    出参：JSONResponse，body 为 {"ok": False, "data": None, "error": {"code": code, "msg": msg}}。
    失败行为：无（本身就是失败出口）；不回传堆栈等内部细节，避免信息泄露。
    """
    body = {"ok": False, "data": None,                   # 失败体：data 恒为 None
            "error": {"code": code, "msg": msg}}         # 错误对象：业务码 + 中文说明
    return JSONResponse(status_code=http_status, content=body)  # 返回带状态码的响应


def err_body(code: str, msg: str) -> dict:               # 纯字典版失败体（供内部函数/脚本断言用）
    """构造失败响应字典（不包 HTTP 层）。

    入参：code 业务错误码，msg 中文说明。出参：统一失败结构字典。
    失败行为：无。"""
    return {"ok": False, "data": None,                   # 失败体结构
            "error": {"code": code, "msg": msg}}         # 错误明细
