import asyncio  # 用于异步编程，实现高并发处理
import hashlib  # 用于加密，微信公众号接口验证时需要
import json  # 用于JSON数据的序列化和反序列化
import logging  # 用于日志记录，追踪程序运行状态
import time  # 用于时间相关操作，例如计算超时
import xml.etree.ElementTree as ET  # 用于解析XML数据，微信公众号事件通知使用XML格式
from contextlib import (
    asynccontextmanager,  # 用于创建异步上下文管理器，管理应用的生命周期事件
)
from functools import lru_cache  # 用于缓存函数结果，提高性能
from os import getcwd, path  # 用于获取当前工作目录和路径操作
from typing import Any, Dict  # 类型提示，增强代码可读性

from fastapi import (  # FastAPI框架的核心组件
    FastAPI,  # FastAPI应用实例
    HTTPException,  # 用于主动返回HTTP错误响应
    Query,  # 用于声明查询参数
    Request,  # 代表HTTP请求对象
    Response,  # 代表HTTP响应对象
)
from fastapi.middleware.cors import CORSMiddleware  # CORS中间件，用于处理跨域资源共享
from sse_starlette.sse import EventSourceResponse  # 用于实现Server-Sent Events (SSE)

from conf.config_env import SettingsEnv  # 导入应用配置类
from conf.config_yaml import load_from_yml  # 从YAML文件加载配置的工具函数
from log_util import load_log_conf  # 加载日志配置的工具函数
from scene_id_manager import SceneIdManager  # 导入场景ID管理器，核心业务逻辑

# 获取当前脚本的真实根路径，用于定位配置文件等资源。
# 这样做可以确保无论脚本从哪里执行，都能正确找到相对路径的文件。
root = path.realpath(getcwd())


def parse_xml_to_dict(xml_str):
    """
    将微信推送的XML字符串转换为Python字典。
    微信的事件通知是以XML格式发送的，将其转换为字典更便于在Python中处理。
    """
    root_node = ET.fromstring(xml_str)  # ET.fromstring 用于从字符串解析XML
    xml_dict = {}
    for child in root_node:  # 遍历XML的子节点
        xml_dict[child.tag] = child.text  # 将子节点的标签作为键，文本内容作为值
    return xml_dict


# 使用lru_cache装饰器缓存get_settings的返回结果。
# 目的是避免重复加载和实例化配置对象，提高性能，并确保配置的一致性。
@lru_cache()
def get_settings():
    return SettingsEnv()


# --- 应用初始化 ---
# 加载日志配置: 根据环境变量中指定的YAML配置文件来设置日志记录器。
# 这样做可以将日志配置与代码分离，方便根据不同环境（开发、生产）调整日志级别和输出。
log_conf_path = get_settings().log_yaml
log_conf = load_from_yml(root, log_conf_path, dict[str, Any])
load_log_conf(root, log_conf)
logger = logging.getLogger("main")  # 获取名为 "main" 的logger实例，用于后续的日志记录

# 从设置中获取敏感信息和关键配置。
# 将这些配置放在环境变量或配置文件中，而不是硬编码，是出于安全和灵活性考虑。
api_token = get_settings().api_token  # 用于验证自定义API接口的访问权限
appid = get_settings().appid  # 微信公众号的AppID
mp_token = get_settings().mp_token  # 用于微信公众号服务器地址验证的Token
secret_key = (
    get_settings().secret_key
)  # 微信公众号的AppSecret，用于获取access_token等操作


# FastAPI 应用生命周期管理: 使用 asynccontextmanager 定义应用的启动和关闭事件。
# 这允许在应用启动时执行初始化操作（如预创建scene_id），并在关闭时执行清理操作。
@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    # 应用启动时执行的代码
    logger.info("应用启动：开始预创建 scene_id")
    # 预创建一定数量的 scene_id，以提高后续请求二维码接口的响应速度。
    # 这是一个常见的性能优化手段，避免在用户请求时才去调用微信API。
    await scene_id_manager.pre_create_scene_ids(10)  # 预创建10个
    logger.info("应用启动：预创建 scene_id 完成")
    yield  # yield 语句是生命周期管理的关键，之前的代码在启动时运行，之后的代码在关闭时运行
    # 应用关闭时执行的代码 (如果需要)
    logger.info("应用关闭")


# 创建 FastAPI 应用实例，并指定生命周期管理器。
app = FastAPI(
    version="v1.0",  # API版本号
    lifespan=lifespan,  # 关联上面定义的生命周期函数
)

# 添加CORS中间件: 允许所有来源、所有方法、所有请求头的跨域请求。
# 在开发环境中通常设置为宽松，但在生产环境中应根据实际需求配置具体的允许列表，以增强安全性。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源
    allow_credentials=True,  # 允许携带cookies
    allow_methods=["*"],  # 允许所有HTTP方法
    allow_headers=["*"],  # 允许所有请求头
)


# --- 认证与全局实例 ---
def verify_token(token: str):
    """
    验证API请求中的token是否有效。
    这是一个简单的示例，直接比较传入的token和配置中的api_token。
    在实际生产环境中，通常会使用更安全的认证机制，如JWT (JSON Web Tokens)。
    """
    try:
        if token != get_settings().api_token:
            # 如果token不匹配，则抛出HTTPException，FastAPI会自动转换为401 Unauthorized响应。
            raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as e:
        logger.error(f"token 验证失败: {e}")
        # 捕获任何可能的异常，并统一返回401错误。
        raise HTTPException(status_code=401, detail="Invalid token")


# 用于存储SSE客户端连接的全局字典。
# 键是 scene_id，值是对应的 asyncio.Queue。
# 当微信服务器推送扫码事件时，可以通过 scene_id 找到对应的队列，并将用户信息放入队列，
# SSE连接端则从队列中读取信息并发送给前端。
clients: Dict[str, asyncio.Queue] = {}

# 创建 SceneIdManager 的全局单例。
# SceneIdManager 负责管理二维码场景ID的创建、获取和释放，是核心业务逻辑的承担者。
# 在整个应用生命周期中，我们通常只需要一个 SceneIdManager 实例。
scene_id_manager = SceneIdManager()


# --- API 端点 ---


# 微信事件接收接口 (POST)
# 用于接收微信服务器推送的各种事件，例如用户关注、取消关注、扫码等。
@app.post("/wechat_event")
async def post_wechat_event(
    request: Request,  # FastAPI的Request对象，可以获取请求体、头等信息
):
    xml_bytes = await request.body()  # 异步读取请求体（XML数据）
    xml_str = xml_bytes.decode("utf-8")  # 将字节流解码为UTF-8字符串
    logger.info(f"收到微信事件 XML: {xml_str}")
    xml_dict = parse_xml_to_dict(xml_str)  # 将XML解析为字典
    msg_type = xml_dict.get("MsgType")  # 获取消息类型
    logger.info(f"MsgType: {msg_type}")

    # 只处理事件类型的消息
    if msg_type == "event":
        event = xml_dict.get("Event")  # 获取具体的事件类型
        # 处理用户扫描带参数二维码事件 (SCAN) 或新用户关注后扫描带参数二维码事件 (subscribe)
        if event == "SCAN" or event == "subscribe":
            # EventKey 在用户未关注时，为 "qrscene_" 前缀加上二维码的参数值；
            # 在用户已关注时，为二维码的参数值。
            # 这里直接使用 EventKey 作为 scene_id，需要 SceneIdManager 生成的 scene_id 不包含 "qrscene_" 前缀。
            # 或者在 SceneIdManager 生成时就带上 "qrscene_"，或者在这里做兼容处理。
            # 当前代码直接使用 EventKey，意味着 SceneIdManager 生成的 scene_id 需要与微信推送的 EventKey 格式一致。
            raw_event_key = xml_dict.get("EventKey", "")
            scene_id = raw_event_key.replace(
                "qrscene_", ""
            )  # 移除 "qrscene_" 前缀（如果存在）

            if not scene_id:
                logger.warning("无法从 EventKey 中获取有效的 scene_id")
                # 返回空字符串的纯文本响应，告知微信服务器已收到，避免重试。
                return Response(content="", media_type="text/plain")

            ticket = xml_dict.get(
                "Ticket"
            )  # 获取用于换取二维码图片的ticket，这里主要用于日志记录
            logger.info(
                f"处理扫码/关注事件 EventKey(raw): {raw_event_key}, scene_id: {scene_id}, Ticket: {ticket}"
            )

            # 检查此 scene_id 是否对应一个活动的SSE客户端连接
            if scene_id in clients:
                try:
                    user_id = xml_dict.get("FromUserName")  # 获取扫码用户的OpenID
                    # 将用户信息（OpenID和事件类型）放入与scene_id关联的队列中
                    # SSE连接会监听这个队列，并将消息推送给前端
                    await clients[scene_id].put({"userId": user_id, "event": event})
                    logger.info(
                        f"已将用户信息 {user_id} 推送到 scene_id {scene_id} 的队列"
                    )
                    # 成功处理后，释放该 scene_id，使其可以被复用
                    await scene_id_manager.release_scene_id(scene_id)
                    return Response(content="", media_type="text/plain")
                except Exception as e:
                    logger.error(f"向队列发送消息失败 (scene_id: {scene_id}): {e}")
                    return Response(content="", media_type="text/plain")
            else:
                # 如果没有找到对应的SSE客户端连接，说明该二维码可能已过期或已被处理
                logger.warning(f"未找到 scene_id: {scene_id} 对应的活动SSE客户端队列")
                return Response(content="", media_type="text/plain")

    # 对于其他类型的消息或事件，直接返回空响应
    return Response(content="", media_type="text/plain")


# SSE 连接的总超时时间（秒）
# 如果SSE连接在这个时间内没有收到任何微信扫码事件，服务端会主动断开连接。
total_timeout = 20


# Server-Sent Events (SSE) 接口
# 前端通过此接口建立长连接，等待扫码结果。
@app.get("/sse")
async def sse_endpoint(
    request: Request, token: str = Query(...)
):  # token通过查询参数传入
    verify_token(token)  # 首先验证请求的token是否合法

    # 从 SceneIdManager 获取一个新的 scene_id 和对应的二维码URL
    # 这是整个流程的起点：前端请求SSE -> 后端生成二维码 -> 前端展示二维码 -> 用户扫码 -> 微信推事件 -> 后端处理事件 -> 通过SSE通知前端
    scene_id, qrcode_url = await scene_id_manager.get_scene_id()
    if scene_id is None or qrcode_url is None:
        # 如果无法获取 scene_id (例如 SceneIdManager 内部创建失败且达到重试上限)
        # 返回503服务不可用错误，提示客户端稍后重试。
        return Response(
            content="服务繁忙，请稍后重试", media_type="text/plain", status_code=503
        )

    logger.info(f"SSE客户端连接建立，分配 scene_id: {scene_id}")
    queue = asyncio.Queue()  # 为这个新的SSE连接创建一个异步队列
    clients[scene_id] = queue  # 将 scene_id 和队列关联起来，存入全局字典

    async def event_generator(initial_qrcode_data, client_queue, current_scene_id):
        """
        SSE事件生成器。
        这是一个异步生成器，用于向客户端持续发送事件。
        """
        try:
            # 1. 首先立即发送包含 scene_id 和 qrcode_url 的初始数据
            #    前端收到后会展示二维码。
            yield dict(data=json.dumps(initial_qrcode_data, ensure_ascii=False))

            start_time = time.time()  # 记录连接开始时间，用于计算超时

            # 2. 进入循环，等待从队列中获取微信扫码事件的数据
            while True:
                try:
                    # 计算剩余等待时间
                    elapsed_time = time.time() - start_time
                    remaining_time = total_timeout - elapsed_time

                    if remaining_time <= 0:
                        # 如果总等待时间超过 total_timeout，则认为连接超时
                        logger.info(
                            f"SSE连接超时 (scene_id: {current_scene_id})，服务端主动断开。"
                        )
                        # 发送一个特定的超时事件给前端（可选）
                        yield dict(
                            data=json.dumps(
                                {
                                    "event": "timeout",
                                    "message": "Connection timed out by server.",
                                },
                                ensure_ascii=False,
                            )
                        )
                        await asyncio.sleep(0.1)  # 短暂等待，确保消息发出
                        break  # 跳出循环，结束SSE连接

                    # 等待从队列中获取数据，设置单次等待超时（也使用remaining_time，或一个较小值如1秒）
                    # asyncio.wait_for 会在超时后引发 asyncio.TimeoutError
                    # 这里使用 remaining_time 作为 wait_for 的超时，确保不会超过总超时
                    data_from_queue = await asyncio.wait_for(
                        client_queue.get(), timeout=remaining_time
                    )

                    logger.info(
                        f"从队列获取到数据 (scene_id: {current_scene_id}): {data_from_queue}"
                    )
                    # 将从队列中获取的数据（即微信扫码用户的OpenID等）发送给客户端
                    yield dict(data=json.dumps(data_from_queue, ensure_ascii=False))
                    # 收到有效数据后，通常意味着扫码成功，可以结束SSE连接
                    # 如果设计为一次扫码后即断开，则在此处 break
                    # 如果设计为可以多次扫码（不常见于此场景），则不 break
                    break  # 当前设计为一次扫码成功后即断开

                except asyncio.TimeoutError:
                    # 这是 asyncio.wait_for(queue.get(), ...) 的超时
                    # 意味着在 remaining_time 内没有从队列等到新消息
                    # 这通常也意味着整体连接超时，因为 remaining_time 会趋近于0
                    logger.info(f"等待队列数据超时 (scene_id: {current_scene_id})")
                    # 发送一个特定的超时事件给前端（可选）
                    yield dict(
                        data=json.dumps(
                            {
                                "event": "timeout",
                                "message": "No scan event received in time.",
                            },
                            ensure_ascii=False,
                        )
                    )
                    await asyncio.sleep(0.1)
                    break  # 跳出循环，结束SSE连接
                except asyncio.CancelledError:
                    # 当客户端断开连接时，FastAPI/Starlette 会取消这个协程
                    logger.info(
                        f"SSE客户端断开连接或任务被取消 (scene_id: {current_scene_id})"
                    )
                    raise  # 重新抛出CancelledError，以便外层正确处理
                except Exception as e:
                    logger.error(
                        f"SSE事件生成器发生异常 (scene_id: {current_scene_id}): {e}"
                    )
                    break  # 发生其他异常时也断开连接
        finally:
            # 清理操作：无论连接如何结束（正常完成、超时、客户端断开、异常），都执行此块
            logger.info(f"SSE连接关闭，执行清理 (scene_id: {current_scene_id})")
            if current_scene_id in clients:
                # 从全局字典中移除此 scene_id 对应的队列
                del clients[current_scene_id]
                logger.info(f"已从 clients 中移除 scene_id: {current_scene_id}")
            # 释放 scene_id，使其可以被 SceneIdManager 回收复用
            # 即使之前在 post_wechat_event 中释放过，这里也需要释放，
            # 因为连接可能因为超时或客户端主动断开而结束，此时 post_wechat_event 可能还未执行。
            # SceneIdManager 内部应能处理重复释放的情况（例如，通过检查ID是否仍在“已分配”状态）。
            await scene_id_manager.release_scene_id(current_scene_id)
            logger.info(f"已释放 scene_id: {current_scene_id} (来自SSE finally块)")

    # 准备要立即发送给客户端的初始数据
    initial_data_to_send = {"scene_id": scene_id, "qrcode_url": qrcode_url}
    # 设置CORS头部，确保SSE连接的跨域许可
    response_headers = {"Access-Control-Allow-Origin": "*"}
    return EventSourceResponse(
        event_generator(initial_data_to_send, queue, scene_id), headers=response_headers
    )


# 微信公众号服务器地址验证接口 (GET)
# 当在微信公众号后台配置服务器地址时，微信会向此接口发送一个GET请求以验证服务器的有效性。
@app.get("/wechat_event")
async def get_wechat_event(
    request: Request,
    signature: str = Query(None),  # 微信加密签名
    timestamp: str = Query(None),  # 时间戳
    nonce: str = Query(None),  # 随机数
    echostr: str = Query(None),  # 随机字符串，验证成功后需原样返回
):
    # 验证逻辑：
    # 1. 将token、timestamp、nonce三个参数进行字典序排序。
    # 2. 将三个参数字符串拼接成一个字符串进行sha1加密。
    # 3. 开发者获得加密后的字符串可与signature对比，标识该请求来源于微信。
    logger.debug("接收到微信服务器地址验证请求")
    if not all([mp_token, timestamp, nonce, signature, echostr]):
        logger.warning("微信服务器验证参数不完整")
        return Response(content="参数缺失", media_type="text/plain", status_code=400)

    params_list = [mp_token, timestamp, nonce]
    params_list.sort()  # 字典序排序
    # 拼接并进行SHA1加密
    hashcode = hashlib.sha1("".join(params_list).encode("utf-8")).hexdigest()

    logger.debug(f"计算得到的哈希: {hashcode}, 微信传入的签名: {signature}")
    if hashcode == signature:
        # 如果签名一致，说明请求来自微信服务器，原样返回echostr
        logger.info("微信服务器地址验证成功")
        return Response(content=echostr, media_type="text/plain")
    else:
        # 签名不一致，验证失败
        logger.warning("微信服务器地址验证失败")
        return Response(content="非法请求!", media_type="text/plain", status_code=400)
