import asyncio
import logging
import time
import uuid
from functools import lru_cache
from typing import Dict, List
from urllib.parse import quote

import httpx

from conf.config_env import SettingsEnv

logger = logging.getLogger(__name__)


@lru_cache()
def get_settings():
    return SettingsEnv()


class SceneIdManager:
    def __init__(self):
        self.scene_id_to_url: Dict[str, Dict[str, str]] = {}
        self.lock = asyncio.Lock()
        self.appid = get_settings().appid
        self.secret_key = get_settings().secret_key
        self.max_retries = 3  # 最大重试次数
        self.retry_delay = 1  # 重试间隔（秒）
        self.expire_seconds = 2592000  # 二维码有效期30天
        self.available_scene_ids: List[str] = []

    async def get_stable_access_token(self):
        token_url = "https://api.weixin.qq.com/cgi-bin/stable_token"  # WeChat API URL for token retrieval

        data = {
            "grant_type": "client_credential",
            "appid": self.appid,
            "secret": self.secret_key,
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(token_url, json=data)
            response_data = response.json()

        if "access_token" in response_data:
            return response_data
        else:
            logger.error(
                f"获取稳定访问令牌失败: {response_data.get('errmsg', 'Failed to retrieve access token')}"
            )
            return None

    async def _create_qrcode(self, scene_id: str) -> Dict[str, str] | None:
        token_response = await self.get_stable_access_token()
        if not token_response:
            return None
        access_token = token_response.get("access_token")

        url = f"https://api.weixin.qq.com/cgi-bin/qrcode/create?access_token={access_token}"
        payload = {
            "expire_seconds": self.expire_seconds,  # 使用类的过期时间
            "action_name": "QR_STR_SCENE",
            "action_info": {"scene": {"scene_str": scene_id}},
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload)
            response_data = response.json()

        if "errcode" in response_data and response_data["errcode"] != 0:
            logger.error(
                f"创建二维码失败: {response_data.get('errmsg', 'Failed to create QR code')}"
            )
            return None

        # 返回的是微信的 ticket，需要进一步转换为 URL
        ticket = response_data.get("ticket")
        if not ticket:
            logger.error("创建二维码失败，未获取到 ticket")
            return None
        qrcode_url = (
            f"https://mp.weixin.qq.com/cgi-bin/showqrcode?ticket={quote(ticket)}"
        )
        return {"qrcode_url": qrcode_url, "created_at": str(int(time.time()))}

    async def pre_create_scene_ids(self, count: int):
        """预创建 scene_id"""
        for _ in range(count):
            scene_id = str(uuid.uuid4())
            logger.info(f"预创建 scene_id: {scene_id}")
            result = await self._create_qrcode(scene_id)
            if result:
                qrcode_url = result["qrcode_url"]
                created_at = result["created_at"]
                self.scene_id_to_url[scene_id] = {
                    "qrcode_url": qrcode_url,
                    "created_at": created_at,
                }
                self.available_scene_ids.append(scene_id)
                logger.info(f"预创建二维码成功: {scene_id} -> {qrcode_url}")
            else:
                logger.warning(f"预创建二维码失败: {scene_id}")

    async def get_scene_id(self) -> tuple[str, str] | None:
        async with self.lock:
            while self.available_scene_ids:
                scene_id = self.available_scene_ids.pop()
                logger.info(f"从列表中获取 scene_id: {scene_id}")
                data = self.scene_id_to_url.get(scene_id)
                if data:
                    qrcode_url = data["qrcode_url"]
                    created_at = int(data["created_at"])
                    if time.time() - created_at < self.expire_seconds:
                        return scene_id, qrcode_url
                    else:
                        logger.info(f"scene_id 已过期: {scene_id}")
                        del self.scene_id_to_url[scene_id]  # 删除过期记录

            new_scene_id = str(uuid.uuid4())
            logger.info(f"生成新的 scene_id: {new_scene_id}")
            for attempt in range(self.max_retries):
                result = await self._create_qrcode(new_scene_id)
                if result:
                    qrcode_url = result["qrcode_url"]
                    created_at = result["created_at"]
                    self.scene_id_to_url[new_scene_id] = {
                        "qrcode_url": qrcode_url,
                        "created_at": created_at,
                    }
                    logger.info(f"创建二维码成功: {new_scene_id} -> {qrcode_url}")
                    return new_scene_id, qrcode_url
                else:
                    logger.warning(f"第 {attempt + 1} 次尝试创建二维码失败")
                    await asyncio.sleep(self.retry_delay)

            logger.error(f"达到最大重试次数，创建二维码失败: {new_scene_id}")
            return None, None

    async def release_scene_id(self, scene_id: str):
        async with self.lock:
            if scene_id in self.scene_id_to_url:
                self.available_scene_ids.append(scene_id)
                logger.info(f"释放 scene_id: {scene_id}")
            else:
                logger.warning(f"尝试释放不存在的 scene_id: {scene_id}")

    def get_qrcode_url(self, scene_id: str) -> str | None:
        data = self.scene_id_to_url.get(scene_id)
        return data.get("qrcode_url") if data else None
