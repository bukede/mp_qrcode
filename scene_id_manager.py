import asyncio
import logging
import time
import uuid
from functools import lru_cache  # 导入lru_cache装饰器，用于缓存函数结果，避免重复计算
from typing import Dict, List  # 类型提示，增强代码可读性和健壮性
from urllib.parse import quote  # 用于URL编码，确保ticket在URL中正确传递

import httpx  # 异步HTTP客户端库，用于高效地进行网络请求

from conf.config_env import SettingsEnv  # 导入配置类，用于获取应用设置

logger = logging.getLogger(__name__)  # 获取一个logger实例，用于记录程序运行信息


# 使用lru_cache装饰器缓存get_settings的返回结果
# 这样做是为了避免每次调用get_settings时都重新实例化SettingsEnv，
# 从而提高性能，并确保在应用生命周期内配置的一致性。
@lru_cache()
def get_settings():
    return SettingsEnv()


class SceneIdManager:
    """
    管理微信公众号二维码场景ID的类。
    主要职责包括：
    1. 获取微信稳定版接口调用凭证 (access_token)。
    2. 创建带参数的二维码，并将场景ID与二维码URL、创建时间等信息关联存储。
    3. 预创建一批场景ID及其对应的二维码，以提高获取二维码的响应速度。
    4. 提供获取可用场景ID和对应二维码URL的接口，优先使用预创建且未过期的ID。
    5. 当预创建ID不足或过期时，动态创建新的场景ID和二维码。
    6. 提供释放场景ID的接口，使其可以被复用。
    7. 通过异步操作和锁机制保证在高并发场景下的数据一致性和性能。
    """

    def __init__(self):
        # 存储 scene_id 到其对应二维码信息（包括URL和创建时间戳）的映射。
        # 这样做是为了能够快速通过 scene_id 查找到二维码URL，并判断其是否过期。
        self.scene_id_to_url: Dict[str, Dict[str, str]] = {}
        # 异步锁，用于保护共享资源（如 scene_id_to_url 和 available_scene_ids）的并发访问。
        # 在异步环境中，多个协程可能同时修改这些数据，使用锁可以防止数据竞争和不一致。
        self.lock = asyncio.Lock()
        # 从配置中获取微信公众号的 appid。
        # 将敏感配置外部化管理，而不是硬编码在代码中，是良好的安全实践。
        self.appid = get_settings().appid
        # 从配置中获取微信公众号的 secret_key。
        self.secret_key = get_settings().secret_key
        # 创建二维码失败时的最大重试次数。
        # 网络请求可能由于瞬时问题失败，重试机制可以提高操作的成功率和系统的健壮性。
        self.max_retries = 3
        # 每次重试之间的延迟时间（秒）。
        # 设置延迟是为了避免在API服务暂时不可用时过于频繁地发起请求，给服务器造成更大压力。
        self.retry_delay = 1
        # 生成的二维码的有效期（秒），这里设置为30天。
        # 微信临时二维码有有效期限制，合理设置有效期可以平衡资源使用和业务需求。
        self.expire_seconds = 2592000
        # 存储当前可用的、已预创建的 scene_id 列表。
        # 这是一个ID池，用于快速分配，提高 `get_scene_id` 的效率。
        self.available_scene_ids: List[str] = []

    async def get_stable_access_token(self):
        """
        获取微信接口的稳定版 access_token。
        微信提供了多种获取 access_token 的方式，稳定版接口旨在提供更长有效期和更可靠的 token，
        减少因 token 失效或频繁刷新带来的问题。
        """
        token_url = "https://api.weixin.qq.com/cgi-bin/stable_token"

        data = {
            "grant_type": "client_credential",  # 授权类型，固定为 client_credential
            "appid": self.appid,
            "secret": self.secret_key,
        }

        # 使用 httpx 进行异步POST请求，以非阻塞方式获取 token。
        async with httpx.AsyncClient() as client:
            response = await client.post(token_url, json=data)
            response_data = response.json()

        if "access_token" in response_data:
            return (
                response_data  # 成功获取到 token，返回包含 token 和过期时间等信息的字典
            )
        else:
            # 获取失败，记录错误日志。
            # 详细的错误信息有助于排查问题，例如 appid/secret 配置错误、IP白名单问题等。
            logger.error(
                f"获取稳定访问令牌失败: {response_data.get('errmsg', 'Failed to retrieve access token')}"
            )
            return None

    async def _create_qrcode(self, scene_id: str) -> Dict[str, str] | None:
        """
        内部方法，根据给定的 scene_id 创建一个微信二维码。
        这个方法被设计为私有的（通过下划线前缀约定），因为它主要由类内部的其他方法调用。
        返回包含二维码URL和创建时间戳的字典，如果创建失败则返回None。
        """
        # 首先获取 access_token，因为调用微信大部分API都需要它。
        token_response = await self.get_stable_access_token()
        if not token_response:
            # 如果获取 token 失败，则无法创建二维码。
            return None
        access_token = token_response.get("access_token")

        url = f"https://api.weixin.qq.com/cgi-bin/qrcode/create?access_token={access_token}"
        payload = {
            "expire_seconds": self.expire_seconds,  # 使用类中定义的二维码有效期。
            "action_name": "QR_STR_SCENE",  # 表示创建一个携带字符串参数的临时二维码。
            # "QR_SCENE" 为数字参数的临时二维码，
            # "QR_LIMIT_SCENE" 为数字参数的永久二维码，
            # "QR_LIMIT_STR_SCENE" 为字符串参数的永久二维码。
            # 此处虽然设置了 expire_seconds，但 action_name 决定了场景值类型。
            "action_info": {
                "scene": {"scene_str": scene_id}
            },  # 场景信息，scene_str 即为二维码携带的参数。
        }

        # 异步POST请求创建二维码。
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload)
            response_data = response.json()

        # 检查微信API返回的错误码。errcode为0表示成功。
        if "errcode" in response_data and response_data["errcode"] != 0:
            logger.error(
                f"创建二维码失败: {response_data.get('errmsg', 'Failed to create QR code')}"
            )
            return None

        # 微信API成功创建二维码后，会返回一个 ticket。
        # 这个 ticket 需要用来换取真正的二维码图片URL。
        ticket = response_data.get("ticket")
        if not ticket:
            logger.error("创建二维码失败，未获取到 ticket")
            return None

        # 将 ticket 进行URL编码，然后拼接到微信提供的展示二维码的URL上。
        # quote 函数确保 ticket 中的特殊字符被正确编码，不会破坏URL结构。
        qrcode_url = (
            f"https://mp.weixin.qq.com/cgi-bin/showqrcode?ticket={quote(ticket)}"
        )
        # 返回二维码URL和当前的Unix时间戳（作为创建时间）。
        # 创建时间用于后续判断二维码是否过期。
        return {"qrcode_url": qrcode_url, "created_at": str(int(time.time()))}

    async def pre_create_scene_ids(self, count: int):
        """
        预创建指定数量的 scene_id 及其对应的二维码。
        目的是为了提高后续 `get_scene_id` 方法的响应速度。当系统需要二维码时，
        可以直接从预创建的池中获取，避免了实时调用微信API创建二维码的延迟。
        这对于需要快速响应用户请求的场景非常有用。
        同时，这也有助于平滑API调用，避免在短时间内集中大量请求微信接口。
        """
        for _ in range(count):
            # 生成一个全局唯一的 scene_id。
            scene_id = str(uuid.uuid4())
            logger.info(f"预创建 scene_id: {scene_id}")
            result = await self._create_qrcode(scene_id)
            if result:
                qrcode_url = result["qrcode_url"]
                created_at = result["created_at"]
                # 将成功创建的 scene_id 及其信息存入映射表和可用列表。
                # 注意：这里没有加锁，因为 pre_create_scene_ids 通常在应用启动时单次调用，
                # 或者由一个专门的后台任务调用，并发冲突的风险较低。
                # 如果在并发场景下调用此方法，则需要考虑加锁。
                self.scene_id_to_url[scene_id] = {
                    "qrcode_url": qrcode_url,
                    "created_at": created_at,
                }
                self.available_scene_ids.append(scene_id)
                logger.info(f"预创建二维码成功: {scene_id} -> {qrcode_url}")
            else:
                logger.warning(f"预创建二维码失败: {scene_id}")

    async def get_scene_id(self) -> tuple[str, str] | None:
        """
        获取一个可用的 scene_id 和对应的二维码 URL。
        获取策略：
        1. 优先从 `available_scene_ids` (预创建池) 中获取。
        2. 检查获取到的ID是否过期，如果过期则丢弃并尝试获取下一个。
        3. 如果预创建池为空或所有ID都已过期，则动态生成一个新的 scene_id 并为其创建二维码。
        4. 动态创建时，如果失败会进行重试。
        返回一个元组 (scene_id, qrcode_url)，如果最终无法获取则返回 (None, None)。
        """
        # 使用异步锁确保对 available_scene_ids 和 scene_id_to_url 的操作是线程安全的。
        # 这是因为多个请求可能同时调用 get_scene_id。
        async with self.lock:
            # 尝试从预创建的可用ID列表中获取
            while self.available_scene_ids:
                scene_id = (
                    self.available_scene_ids.pop()
                )  # 从列表尾部弹出一个ID，效率较高
                logger.info(f"从列表中获取 scene_id: {scene_id}")
                data = self.scene_id_to_url.get(scene_id)
                if data:
                    qrcode_url = data["qrcode_url"]
                    created_at = int(data["created_at"])
                    # 检查二维码是否仍在有效期内。
                    # 这样做是为了确保返回给用户的二维码是可用的。
                    if time.time() - created_at < self.expire_seconds:
                        return scene_id, qrcode_url
                    else:
                        # 如果ID已过期，则从主映射中删除，避免内存泄漏和后续误用。
                        logger.info(f"scene_id 已过期: {scene_id}")
                        del self.scene_id_to_url[scene_id]

            # 如果预创建列表为空或所有ID都已过期，则需要动态创建一个新的 scene_id。
            new_scene_id = str(uuid.uuid4())
            logger.info(f"生成新的 scene_id: {new_scene_id}")
            # 尝试创建二维码，包含重试逻辑。
            # 这样做是为了提高在网络不稳定或微信API暂时故障情况下的成功率。
            for attempt in range(self.max_retries):
                result = await self._create_qrcode(new_scene_id)
                if result:
                    qrcode_url = result["qrcode_url"]
                    created_at = result["created_at"]
                    # 将新创建的 scene_id 存入映射表。
                    # 注意：这里不需要再将其加入 available_scene_ids，因为它已被立即分配出去。
                    self.scene_id_to_url[new_scene_id] = {
                        "qrcode_url": qrcode_url,
                        "created_at": created_at,
                    }
                    logger.info(f"创建二维码成功: {new_scene_id} -> {qrcode_url}")
                    return new_scene_id, qrcode_url
                else:
                    logger.warning(f"第 {attempt + 1} 次尝试创建二维码失败")
                    # 在重试前等待一段时间，避免过于频繁地请求。
                    await asyncio.sleep(self.retry_delay)

            # 如果达到最大重试次数仍然失败，则记录错误并返回 None。
            logger.error(f"达到最大重试次数，创建二维码失败: {new_scene_id}")
            return None, None

    async def release_scene_id(self, scene_id: str):
        """
        释放一个 previously 获取的 scene_id，使其可以被重新利用。
        当一个场景ID对应的业务流程结束，或者该ID不再需要时，可以调用此方法。
        释放的ID会被添加回 `available_scene_ids` 列表，供后续 `get_scene_id` 调用时复用。
        这样做有助于减少对微信API创建二维码接口的调用次数，节约资源。
        """
        async with self.lock:  # 同样需要锁来保护共享列表的并发修改
            if scene_id in self.scene_id_to_url:
                # 只有当 scene_id 确实存在于我们的管理记录中时，才将其加回可用列表。
                # 避免将无效或外部的ID错误地加入。
                self.available_scene_ids.append(scene_id)
                logger.info(f"释放 scene_id: {scene_id}")
            else:
                # 如果尝试释放一个不存在的ID，记录警告。
                # 这可能表示逻辑错误或尝试释放一个从未被管理的ID。
                logger.warning(f"尝试释放不存在的 scene_id: {scene_id}")

    def get_qrcode_url(self, scene_id: str) -> str | None:
        """
        根据 scene_id 获取其对应的二维码图片 URL。
        这是一个简单的查询方法，用于在已知 scene_id 的情况下获取其二维码。
        注意：此方法不检查二维码是否过期，调用者如果关心有效期，应自行处理或结合其他信息判断。
        """
        data = self.scene_id_to_url.get(scene_id)
        return data.get("qrcode_url") if data else None
