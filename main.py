import asyncio
import hashlib
import json
import logging
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from functools import lru_cache
from os import getcwd, path
from typing import Any

from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from conf.config_env import SettingsEnv
from conf.config_yaml import load_from_yml
from log_util import load_log_conf
from scene_id_manager import SceneIdManager

root = path.realpath(getcwd())


def parse_xml_to_dict(xml_str):
    root = ET.fromstring(xml_str)
    xml_dict = {}
    for child in root:
        xml_dict[child.tag] = child.text
    return xml_dict


@lru_cache()
def get_settings():
    return SettingsEnv()


# 加载日志配置
log_conf = load_from_yml(root, get_settings().log_yaml, dict[str, Any])
load_log_conf(root, log_conf)
logger = logging.getLogger("main")

# 从设置中获取密钥和区域信息
api_token = get_settings().api_token  # 自己程序的验权
appid = get_settings().appid
mp_token = get_settings().mp_token  # 公众号的验权
secret_key = get_settings().secret_key

app = FastAPI(
    version="v1.0",
)
# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# 假设您有一个 verify_token 函数用于验证 JWT
def verify_token(token: str):
    try:
        if token != get_settings().api_token:  # 简单鉴权
            raise HTTPException(status_code=401, detail="Invalid token")

    except Exception as e:
        logger.error(f"token 验证失败: {e}")
        raise HTTPException(status_code=401, detail="Invalid token")


# 用于存储客户端连接的全局字典
clients = {}
# 创建 SceneIdManager 实例
scene_id_manager = SceneIdManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行的代码
    logger.info("开始预创建 scene_id")
    await scene_id_manager.pre_create_scene_ids(10)
    logger.info("预创建 scene_id 完成")
    yield
    # 关闭时执行的代码


app = FastAPI(
    version="v1.0",
    lifespan=lifespan,
)


# 微信事件接收接口
@app.post("/wechat_event")
async def post_wechat_event(
    request: Request,
):
    xml_bytes = await request.body()
    xml_str = xml_bytes.decode("utf-8")
    logger.info(f"收到微信事件 XML: {xml_str}")
    xml_dict = parse_xml_to_dict(xml_str)
    msg_type = xml_dict.get("MsgType")
    logger.info(f"MsgType: {msg_type}")

    if msg_type == "event":
        event = xml_dict.get("Event")
        if event == "SCAN" or event == "subscribe":
            scene_id = f"{xml_dict.get('EventKey')}"
            if not scene_id:
                logger.warning("无法获取 scene_id (FromUserName)")
                return Response(content="", media_type="text/plain")
            ticket = xml_dict.get("Ticket")
            logger.info(f"扫描带参数二维码事件 EventKey: {scene_id}, Ticket: {ticket}")

            if scene_id in clients:
                try:
                    userId = xml_dict.get("FromUserName")
                    await clients[scene_id].put({"userId": userId, "event": event})
                    # 释放 scene_id
                    await scene_id_manager.release_scene_id(scene_id)
                    return Response(content="", media_type="text/plain")
                except Exception as e:
                    logger.error(f"向队列发送消息失败: {e}")
                    return Response(content="", media_type="text/plain")
            else:
                logger.warning(f"未找到 scene_id: {scene_id} 对应的队列")
                return Response(content="", media_type="text/plain")

    return Response(content="", media_type="text/plain")


# SSE 超时时间
total_timeout = 20


# SSE 接口
@app.get("/sse")
async def sse(request: Request, token: str = Query(...)):
    verify_token(token)
    scene_id, qrcode_url = await scene_id_manager.get_scene_id()
    if scene_id is None:
        return Response(
            content="服务繁忙，请稍后重试", media_type="text/plain", status_code=503
        )

    logger.info(f"客户端连接: {scene_id}")
    queue = asyncio.Queue()
    clients[scene_id] = queue

    async def event_generator(initial_data, queue, scene_id):
        try:
            # 首先发送 initial_data
            yield dict(data=json.dumps(initial_data, ensure_ascii=False))
            # 记录连接开始时间
            start_time = time.time()

            while True:
                try:
                    elapsed_time = time.time() - start_time
                    remaining_time = total_timeout - elapsed_time
                    logger.info(f"连接剩余时间: {remaining_time}")
                    if remaining_time <= 0:
                        logger.info(f"连接超时 scene_id: {scene_id} 服务端主动断开连接")
                        await asyncio.sleep(1)
                        break
                    data = await asyncio.wait_for(queue.get(), timeout=total_timeout)

                    logger.info(f"从队列中获取数据 scene_id: {scene_id}, data: {data}")
                    yield dict(data=json.dumps(data, ensure_ascii=False))
                except asyncio.TimeoutError:
                    logger.info(f"队列或连接超时 scene_id: {scene_id}")
                    break
                except asyncio.CancelledError:
                    logger.info(f"客户端断开连接或任务取消 scene_id: {scene_id}")
                    break
                    # No need to break here, the finally block will handle cleanup
                except Exception as e:  # 捕获其他异常
                    logger.error(
                        f"从队列获取数据时发生异常 scene_id: {scene_id}, error: {e}"
                    )
                    break  # 发生其他异常时也断开连接
        finally:
            # 确保连接关闭时释放 scene_id
            logger.info(f"进入 finally 块, scene_id: {scene_id}")
            if scene_id in clients:
                logger.info(f"从 clients 中删除 scene_id: {scene_id}")
                del clients[scene_id]
            logger.info(f"释放 scene_id: {scene_id}")
            await scene_id_manager.release_scene_id(scene_id)
            logger.info(f"客户端连接关闭: {scene_id}")

    # 立即发送 scene_id 和 qrcode_url
    initial_data = {"scene_id": scene_id, "qrcode_url": qrcode_url}
    headers = {"Access-Control-Allow-Origin": "*"}
    return EventSourceResponse(
        event_generator(initial_data, queue, scene_id), headers=headers
    )


# 验证服务器地址有效性接口
@app.get("/wechat_event")
async def get_wechat_event(
    request: Request,
    signature: str = Query(None),
    timestamp: str = Query(None),
    nonce: str = Query(None),
    echostr: str = Query(None),
):
    # 服务器地址验证
    logger.debug("start mp verify")
    params_list = [mp_token, timestamp, nonce]
    params_list.sort()
    hashcode = hashlib.sha1("".join(params_list).encode("utf-8")).hexdigest()

    logger.debug(f"hashcode:{hashcode} signature:{signature}")
    if hashcode == signature:
        return Response(content=echostr, media_type="text/plain")
    else:
        return Response(content="非法请求!", media_type="text/plain", status_code=400)
