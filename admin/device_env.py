"""独立干净设备环境生成器 (Clean Isolated Device Environment)

为每一次手机号注册与登录生成完全独立的设备指纹、网络特征、浏览器头与客户端标识，
避免同一机器/IP/设备在腾讯 OneID 触发风控、频繁限制或关联判定。

所有设置严格限定在会话级的 HTTP 客户端内，绝不修改宿主系统全局网络配置与 DNS。
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from typing import Optional


# 国内三大运营商常见公网 IP 段前缀 (仅用于 X-Forwarded-For/X-Real-IP 模拟出口标识)
_DOMESTIC_IP_PREFIXES = (
    # 广东电信 / 联通 / 移动
    "14.215.", "113.108.", "120.236.", "183.60.",
    # 上海电信 / 联通 / 移动
    "101.80.", "112.64.", "117.131.",
    # 北京联通 / 电信 / 移动
    "123.125.", "106.120.", "111.198.",
    # 浙江移动 / 电信 / 联通
    "111.1.", "122.224.", "124.160.",
    # 江苏电信 / 移动 / 联通
    "114.221.", "120.195.", "122.96.",
    # 四川电信 / 移动
    "118.112.", "183.220.",
    # 山东联通 / 移动
    "119.167.", "112.224.",
)

_ACCEPT_LANGUAGES = (
    "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9",
    "zh-CN,zh-Hans;q=0.9,en-US;q=0.8",
    "zh-CN,zh-TW;q=0.9,zh;q=0.8,en-US;q=0.7",
    "zh-CN,zh-HK;q=0.9,zh;q=0.8,en;q=0.7",
)


@dataclass
class DeviceEnv:
    """单个登录会话的独立设备指纹与网络特征上下文"""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    device_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    client_uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    platform: str = "macOS"  # "macOS" 或 "Windows"
    user_agent: str = ""
    sec_ch_ua: str = ""
    sec_ch_ua_platform: str = ""
    sec_ch_ua_platform_version: str = ""
    accept_language: str = ""
    client_ip: str = ""
    proxy: Optional[str] = None

    def __post_init__(self):
        if not self.user_agent:
            self._generate_random_profile()

    def _generate_random_profile(self) -> None:
        # 随机选取操作系统平台 (macOS 与 Windows 10/11 混编)
        is_mac = random.random() < 0.65
        self.platform = "macOS" if is_mac else "Windows"

        # 随机选取现代 Chrome 主版本号 (130 - 134)
        chrome_major = random.randint(130, 134)
        chrome_patch = f"{chrome_major}.0.{random.randint(6700, 6999)}.{random.randint(50, 199)}"

        if is_mac:
            mac_ver = random.choice(["10_15_7", "14_7_1", "15_2_0", "15_3_1"])
            self.user_agent = (
                f"Mozilla/5.0 (Macintosh; Intel Mac OS X {mac_ver}) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_patch} Safari/537.36"
            )
            self.sec_ch_ua_platform = '"macOS"'
            self.sec_ch_ua_platform_version = f'"{random.choice(["14.7.1", "15.2.0", "15.3.1"])}"'
        else:
            self.user_agent = (
                f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_patch} Safari/537.36"
            )
            self.sec_ch_ua_platform = '"Windows"'
            self.sec_ch_ua_platform_version = f'"{random.choice(["10.0.0", "15.0.0"])}"'

        # 现代规范 Sec-Ch-Ua 标头
        brand_seed = random.choice([
            ('"Chromium";v="{0}", "Google Chrome";v="{0}", "Not?A_Brand";v="99"', chrome_major),
            ('"Google Chrome";v="{0}", "Chromium";v="{0}", "Not-A.Brand";v="24"', chrome_major),
        ])
        self.sec_ch_ua = brand_seed[0].format(brand_seed[1])

        self.accept_language = random.choice(_ACCEPT_LANGUAGES)

        # 随机生成拟真公网 IP 作为单次出口特征
        prefix = random.choice(_DOMESTIC_IP_PREFIXES)
        self.client_ip = f"{prefix}{random.randint(10, 245)}.{random.randint(2, 250)}"

    def build_headers(self, base_host: str, referer: Optional[str] = None, extra: Optional[dict] = None) -> dict[str, str]:
        """构建该独立环境下的专属 HTTP 标头字典"""
        h: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept-Language": self.accept_language,
            "Accept": "application/json, text/plain, */*",
            "Origin": base_host,
            "Referer": referer or (base_host + "/"),
            "Sec-Ch-Ua": self.sec_ch_ua,
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": self.sec_ch_ua_platform,
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "X-Device-Id": self.device_id,
            "X-Client-Id": self.client_uuid,
            "X-Request-Id": uuid.uuid4().hex,
        }

        # 若未走外部代理，注入拟真单次出口 IP，防止集中在同一网关特征
        if not self.proxy:
            h["X-Forwarded-For"] = self.client_ip
            h["X-Real-IP"] = self.client_ip
            h["Client-IP"] = self.client_ip

        if extra:
            h.update(extra)
        return h


def create_clean_env(proxy: Optional[str] = None) -> DeviceEnv:
    """创建全新、独立、干净的设备网络环境"""
    return DeviceEnv(proxy=(proxy or "").strip() or None)
