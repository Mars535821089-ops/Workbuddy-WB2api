"""疾驰短信 (JichiSMS) API 客户端

支持官方引擎（引擎A）与引擎B国内卡取号、读码、释放及项目搜索。
带连接池复用、SSL握手超时自愈重试与高防CDN网络韧性。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.parse
from typing import Any, Optional

import httpx

_logger = logging.getLogger("admin.sms_client")

# 默认接码平台 API 地址与 Token (可由环境变量 JICHI_SMS_TOKEN 或前端界面设置)
DEFAULT_API_URL = "https://www.jichisms.com"
DEFAULT_TOKEN = os.getenv("JICHI_SMS_TOKEN", "")

# 默认 WorkBuddy 推荐项目配置 (139372 实测秒接验证码)
DEFAULT_ENGINE = "a"
DEFAULT_PROJECT_ID_A = "139372"  # 引擎A: workbuddy/codebuddy/腾讯科技二次
DEFAULT_PROJECT_NAME_A = "workbuddy/codebuddy/腾讯科技二次"

DEFAULT_PROJECT_ID_B = "77560"  # 引擎B: 深圳市腾讯计算机系统
DEFAULT_PROJECT_NAME_B = "深圳市腾讯计算机系统"


class SmsClientError(Exception):
    """接码客户端异常"""
    def __init__(self, message: str, code: int = 0, raw: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.raw = raw


class JichiSmsClient:
    def __init__(self, token: str = "", base_url: str = ""):
        self.token = (token or DEFAULT_TOKEN).strip()
        self.base_url = (base_url or DEFAULT_API_URL).rstrip("/")
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)",
            "fcToken": self.token,
            "Connection": "keep-alive",
        }
        self._lock = threading.Lock()
        self._shared_client: Optional[httpx.Client] = None

    def _get_client(self) -> httpx.Client:
        with self._lock:
            if self._shared_client is None or self._shared_client.is_closed:
                self._shared_client = httpx.Client(
                    headers=self.headers,
                    timeout=httpx.Timeout(connect=10.0, read=25.0, write=15.0, pool=15.0),
                    follow_redirects=True,
                    limits=httpx.Limits(max_connections=5, max_keepalive_connections=3, keepalive_expiry=60.0),
                )
            return self._shared_client

    def _recreate_client(self) -> None:
        with self._lock:
            if self._shared_client is not None:
                try:
                    self._shared_client.close()
                except Exception:
                    pass
                self._shared_client = None

    def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """带网络异常自愈与重试的 HTTP 请求包装器"""
        last_exc = None
        for attempt in range(1, 4):
            try:
                client = self._get_client()
                resp = client.request(method, url, **kwargs)
                return resp
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError, httpx.RemoteProtocolError, Exception) as e:
                last_exc = e
                _logger.warning("JichiSMS 请求失败 (尝试 %d/3): %s", attempt, e)
                self._recreate_client()
                if attempt < 3:
                    time.sleep(1.5 * attempt)
        if last_exc:
            raise last_exc
        raise SmsClientError("网络请求异常")

    def search_projects(self, keyword: str = "腾讯", engine: str = "b") -> list[dict]:
        """搜索项目列表"""
        url = f"{self.base_url}/api/user/projects"
        engine = engine.lower().strip()
        params: dict[str, Any] = {"page": 1, "pagesize": 50}

        if engine == "b":
            params["engine"] = "b"
            params["project_name"] = keyword
        else:
            params["project_name"] = keyword

        try:
            resp = self._request_with_retry("GET", url, params=params)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 1:
                return data.get("data") or []
            _logger.warning("搜索项目返回失败: %s", data)
            return []
        except Exception as e:
            _logger.warning("搜索项目异常: %s", e)
            return []

    def get_phone(
        self,
        engine: str = DEFAULT_ENGINE,
        project_id: str = "",
        project_name: str = "",
        isp: str = "",
        province: str = "",
        paragraph: str = "",
        ascription: str = "",
    ) -> dict[str, Any]:
        """获取手机号"""
        engine = engine.lower().strip() or DEFAULT_ENGINE
        if not project_id:
            project_id = DEFAULT_PROJECT_ID_B if engine == "b" else DEFAULT_PROJECT_ID_A
        if engine == "b" and not project_name:
            project_name = DEFAULT_PROJECT_NAME_B

        url = f"{self.base_url}/api/user/getPhone"
        data: dict[str, Any] = {
            "project_id": str(project_id).strip(),
        }

        if engine == "b":
            data["engine"] = "b"
            if project_name:
                data["project_name"] = project_name
        else:
            if isp:
                data["isp"] = isp
            if province:
                data["province"] = province
            if paragraph:
                data["paragraph"] = paragraph
            if ascription:
                data["ascription"] = ascription

        resp = self._request_with_retry("POST", url, data=data)
        resp.raise_for_status()
        res = resp.json()

        if res.get("code") == 1:
            res_data = res.get("data") or {}
            # 兼容 data 为 list 或 dict 的情况
            if isinstance(res_data, list) and res_data:
                res_data = res_data[0]
            phone = str(res_data.get("phone") or "").strip()
            phone = re.sub(r"\D", "", phone)
            if not phone:
                raise SmsClientError("平台未返回有效的手机号", raw=res)
            return {
                "phone": phone,
                "sid": res_data.get("sid") or "",
                "sp": res_data.get("sp") or "",
                "phone_gsd": res_data.get("phone_gsd") or "",
                "raw": res,
            }
        else:
            msg = res.get("msg") or "获取手机号失败"
            raise SmsClientError(msg, code=res.get("code", 0), raw=res)

    def get_verify_code(
        self,
        phone: str,
        project_id: str = "",
        engine: str = DEFAULT_ENGINE,
    ) -> dict[str, Any]:
        """获取验证码（自愈重试不抛出断网异常）"""
        engine = engine.lower().strip() or DEFAULT_ENGINE
        if not project_id:
            project_id = DEFAULT_PROJECT_ID_B if engine == "b" else DEFAULT_PROJECT_ID_A

        url = f"{self.base_url}/api/user/getVerifyCode"
        data: dict[str, Any] = {
            "project_id": str(project_id).strip(),
            "phone": str(phone).strip(),
        }
        if engine == "b":
            data["engine"] = "b"

        try:
            resp = self._request_with_retry("POST", url, data=data)
            resp.raise_for_status()
            res = resp.json()
        except Exception as e:
            return {
                "received": False,
                "code": "",
                "msg": f"网络重试中 ({e})",
                "raw": None,
            }

        if res.get("code") == 1:
            msg = str(res.get("msg") or "").strip()
            # 尝试从 msg 中提取 4~6 位数字验证码（WorkBuddy 通常为 6 位数字）
            digits = re.findall(r"\b(\d{6})\b", msg)
            if not digits:
                digits = re.findall(r"\b(\d{4,6})\b", msg)
            code = digits[0] if digits else msg
            return {
                "received": True,
                "code": code,
                "msg": msg,
                "raw": res,
            }

        msg = str(res.get("msg") or "等待短信中")
        return {
            "received": False,
            "code": "",
            "msg": msg,
            "raw": res,
        }

    def release_phone(
        self,
        phone: str,
        project_id: str = "",
        engine: str = DEFAULT_ENGINE,
    ) -> bool:
        """释放手机号"""
        engine = engine.lower().strip() or DEFAULT_ENGINE
        if not project_id:
            project_id = DEFAULT_PROJECT_ID_B if engine == "b" else DEFAULT_PROJECT_ID_A

        url = f"{self.base_url}/api/user/releasePhone"
        data: dict[str, Any] = {
            "project_id": str(project_id).strip(),
            "phone": str(phone).strip(),
        }
        if engine == "b":
            data["engine"] = "b"

        try:
            resp = self._request_with_retry("POST", url, data=data)
            resp.raise_for_status()
            res = resp.json()
            return res.get("code") == 1
        except Exception as e:
            _logger.warning("释放手机号异常 phone=%s: %s", phone, e)
            return False

    def close(self) -> None:
        self._recreate_client()
