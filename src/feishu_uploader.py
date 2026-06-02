#!/usr/bin/env python3
"""
飞书图片上传模块。
通过飞书自定义应用凭证（app_id + app_secret）获取 tenant_access_token，
再将本地图片上传至飞书图床，返回 img_key 用于交互卡片。

环境变量：
  LARK_APP_ID      飞书自定义应用的 App ID
  LARK_APP_SECRET  飞书自定义应用的 App Secret
"""
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

_TOKEN_URL  = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_UPLOAD_URL = "https://open.feishu.cn/open-apis/im/v1/images"


def _get_tenant_token(app_id: str, app_secret: str) -> str:
    resp = requests.post(
        _TOKEN_URL,
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"tenant_access_token 获取失败：{data}")
    return data["tenant_access_token"]


def upload_chart(image_path: str, app_id: str, app_secret: str) -> Optional[str]:
    """
    上传 PNG 图片到飞书图床，返回 img_key。
    凭证未配置或上传失败时返回 None（主流程继续，跳过图片）。
    """
    if not app_id or not app_secret:
        log.warning("LARK_APP_ID / LARK_APP_SECRET 未配置，无法上传图片，将降级为文字卡片")
        return None
    try:
        token = _get_tenant_token(app_id, app_secret)
        with open(image_path, "rb") as f:
            resp = requests.post(
                _UPLOAD_URL,
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": ("qqq_trend.png", f, "image/png")},
                timeout=30,
            )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            log.error(f"飞书图片上传失败：{data}")
            return None
        img_key = data["data"]["image_key"]
        log.info(f"飞书图片上传成功：{img_key}")
        return img_key
    except Exception as e:
        log.error(f"飞书图片上传出错（跳过图片）：{e}")
        return None
