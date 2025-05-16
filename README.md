# 使用 FastAPI 框架构建了一个 Web 服务，实现通过微信扫描二维码进行识别或验证的流程，并利用 Server-Sent Events (SSE) 将扫码结果实时推送给前端客户端。

**一、应用初始化与配置加载**

在任何 API 请求被处理之前，应用会进行一系列初始化操作：

1.  **导入模块**: 导入 FastAPI、asyncio、logging 等必要的库。
2.  **配置加载**:
    - `get_settings()`: 使用 `lru_cache` 装饰器缓存 `SettingsEnv()` 的实例，该实例负责从环境变量或 `.env` 文件中加载配置（如 API 密钥、微信公众号的 `appid`, `mp_token`, `secret_key` 等）。
    - 日志配置: 从 YAML 文件 (`get_settings().log_yaml`) 加载日志配置，并应用到 `logging` 模块。
3.  **全局变量**:
    - `logger`: 初始化一个名为 "main" 的日志记录器。
    - `api_token`, `appid`, `mp_token`, `secret_key`: 从配置中提取并存储这些关键信息。
    - `clients = {}`: 一个空字典，用于存储客户端 SSE 连接。键是 `scene_id`，值是对应的 `asyncio.Queue`，用于在微信回调和 SSE 连接之间传递消息。
    - `scene_id_manager = SceneIdManager()`: 创建 `SceneIdManager` 的实例。这个管理器负责生成、提供和回收用于微信二维码的场景 ID (`scene_id`)。
4.  **FastAPI 应用创建与中间件**:
    - `app = FastAPI(...)`: 创建 FastAPI 应用实例。
    - **CORS 中间件**: `app.add_middleware(CORSMiddleware, ...)` 添加了 CORS（跨源资源共享）中间件，允许来自任何源 (`allow_origins=["*"]`)、任何方法 (`allow_methods=["*"]`) 和任何头部 (`allow_headers=["*"]`) 的请求。这对于需要从不同域的前端应用访问 API 非常重要。
5.  **生命周期管理 (`lifespan` 函数)**:
    - 通过 `@asynccontextmanager` 定义了一个异步上下文管理器 `lifespan`，并注册到 FastAPI 应用实例。
    - **应用启动时**:
      - 记录日志 "开始预创建 scene_id"。
      - 调用 `await scene_id_manager.pre_create_scene_ids(10)`: 应用启动时会预先创建 10 个场景 ID。这样做可以加快后续请求获取 `scene_id` 的速度，因为不需要实时生成。
      - 记录日志 "预创建 scene_id 完成"。
    - **应用关闭时**: (yield 之后的代码) - 在这个文件中，`lifespan` 函数的 `yield` 之后没有显式的关闭代码，但 SSE 连接的 `finally` 块会负责清理相关资源。

**二、辅助函数**

- `parse_xml_to_dict(xml_str)`:
  - **作用**: 将微信服务器推送过来的 XML 格式的字符串数据解析成 Python 字典。
  - **调用时机**: 在 `/wechat_event` (POST) 接口中接收到微信事件时被调用。
- `verify_token(token: str)`:
  - **作用**: 验证客户端请求 API (特指 `/sse` 接口) 时提供的 `token` 是否有效。
  - **逻辑**: 简单地将传入的 `token` 与从配置中获取的 `get_settings().api_token`进行比较。
  - **失败处理**: 如果 token 无效或验证过程中发生异常，会记录错误日志并抛出 `HTTPException` (状态码 401, "Invalid token")。
  - **调用时机**: 在 `/sse` (GET) 接口的开头被调用，用于接口鉴权。

**三、API 接口执行流程**

应用主要定义了以下几个 API 接口：

1.  **`GET /wechat_event` - 微信服务器地址验证接口**

    - **目的**: 用于微信公众号后台配置服务器地址时的有效性验证。
    - **请求参数**: `signature`, `timestamp`, `nonce`, `echostr` (均来自微信服务器的 Query 参数)。
    - **执行流程**:
      1.  微信服务器向该接口发送 GET 请求。
      2.  接口获取配置中的 `mp_token` (公众号的 Token) 以及请求中的 `timestamp` 和 `nonce`。
      3.  将这三个参数进行字典序排序。
      4.  将排序后的三个参数字符串拼接成一个字符串。
      5.  对拼接后的字符串进行 SHA1 哈希加密。
      6.  将加密后的哈希值与请求中的 `signature` 参数进行对比。
      7.  **验证成功**: 如果哈希值与 `signature` 匹配，则直接返回 `echostr` 参数的内容，表示服务器地址验证通过。
      8.  **验证失败**: 如果不匹配，则返回 "非法请求!" 文本和 400 状态码。

2.  **`POST /wechat_event` - 微信事件接收接口**

    - **目的**: 接收微信服务器推送的各种事件和消息，核心是处理用户扫描带参数二维码的事件。
    - **请求体**: 微信服务器发送的 XML 数据。
    - **执行流程**:
      1.  微信用户扫描了通过本服务生成的带参数二维码。
      2.  微信服务器向此接口发送 POST 请求，请求体为包含事件信息的 XML 数据。
      3.  接口读取请求体中的 XML 数据，解码为 UTF-8 字符串。
      4.  调用 `parse_xml_to_dict()` 将 XML 解析为字典 (`xml_dict`)。
      5.  从 `xml_dict` 中获取 `MsgType` (消息类型)。
      6.  **如果 `MsgType` 是 "event" (事件类型)**:
          - 获取 `Event` (具体事件，如 "SCAN" - 用户已关注时扫描，"subscribe" - 用户未关注时扫描或通过其他方式关注)。
          - **如果 `Event` 是 "SCAN" 或 "subscribe"**:
            - 从 `xml_dict` 的 `EventKey` 字段获取 `scene_id` (场景 ID，即二维码参数)。对于 "subscribe" 事件，`EventKey` 可能带有 "qrscene\_" 前缀，代码直接使用 `f"{xml_dict.get('EventKey')}"`，意味着 `SceneIdManager` 生成的 `scene_id` 需要能匹配这种格式，或者微信处理后传递的是纯数字 ID。
            - 如果 `scene_id` 为空，记录警告并直接返回空响应。
            - 获取 `Ticket` (用于换取二维码图片，此处仅记录日志)。
            - 记录扫码事件的日志。
            - **检查 `scene_id` 是否存在于全局 `clients` 字典中**:
              - **存在**: 说明有前端客户端正在通过 `/sse` 接口等待这个 `scene_id` 的扫码结果。
                - 从 `xml_dict` 的 `FromUserName` 字段获取扫码用户的 OpenID (`userId`)。
                - 将包含 `userId` 和 `event` 类型的数据 `{"userId": userId, "event": event}` 放入与 `scene_id` 关联的 `asyncio.Queue` 中 (`await clients[scene_id].put(...)`)。这将唤醒等待在 `/sse` 接口中对应 `scene_id` 的 `queue.get()`。
                - 调用 `await scene_id_manager.release_scene_id(scene_id)` 释放该 `scene_id`，使其可以被重新使用。
                - 向微信服务器返回一个空的纯文本响应 (微信规范要求)。
              - **不存在**: 说明没有前端客户端在等待这个 `scene_id` (可能 SSE 连接已超时或关闭)。记录警告日志，并返回空响应。
      7.  如果 `MsgType` 不是 "event" 或事件类型不匹配，则直接返回空响应。

3.  **`GET /sse` - Server-Sent Events 接口**
    - **目的**: 供前端客户端调用，获取一个带参数的二维码 URL 和对应的 `scene_id`，然后保持连接，等待微信用户扫描该二维码后的结果（用户 OpenID）。
    - **请求参数**: `token: str` (Query 参数，用于接口认证)。
    - **执行流程**:
      1.  **认证**: 调用 `verify_token(token)` 验证 `token` 的有效性。如果无效，请求被拒绝。
      2.  **获取场景 ID 和二维码**:
          - 调用 `await scene_id_manager.get_scene_id()` 从 `SceneIdManager` 获取一个可用的 `scene_id` 和对应的 `qrcode_url` (二维码图片的链接，可能是微信生成的)。
          - 如果获取失败 (返回 `scene_id` 为 `None`，例如 `SceneIdManager` 中没有可用的 ID)，则返回 503 状态码和 "服务繁忙，请稍后重试" 的消息。
      3.  **注册客户端**:
          - 记录客户端连接日志。
          - 创建一个 `asyncio.Queue()` 实例 (`queue`)。
          - 将这个 `queue` 存储到全局 `clients` 字典中，键为 `scene_id`: `clients[scene_id] = queue`。
      4.  **创建事件生成器 (`event_generator`)**: 这是一个异步生成器函数，负责向客户端发送 SSE 事件。
          - **发送初始数据**:
            - 立即 `yield` (发送) 一个包含 `scene_id` 和 `qrcode_url` 的 JSON 数据。前端收到后会显示二维码。
          - **循环等待扫码结果**:
            - 进入一个 `while True` 循环。
            - **超时控制**:
              - 设置了总超时时间 `total_timeout` (20 秒)。
              - 循环内计算剩余时间，如果超时，记录日志，`break` 循环，服务端主动断开连接。
            - **从队列获取数据**:
              - `data = await asyncio.wait_for(queue.get(), timeout=total_timeout)`: 异步等待从之前注册的 `queue` 中获取数据。这个 `queue.get()` 会阻塞，直到 `/wechat_event` 接口在用户扫码后向该队列中放入数据。`wait_for` 也设置了超时。
              - 如果成功获取到数据 (即用户扫码信息)，记录日志，并将该数据 (包含 `userId`) `yield` 给客户端。
            - **异常处理**:
              - `asyncio.TimeoutError`: 队列等待超时或连接超时，记录日志，`break` 循环。
              - `asyncio.CancelledError`: 客户端断开连接或任务被取消，记录日志，`break` 循环。
              - 其他 `Exception`: 捕获未知异常，记录错误，`break` 循环。
          - **`finally` 清理块**: 无论 `event_generator` 如何退出 (正常结束、超时、异常、客户端断开)，此块都会执行。
            - 从 `clients` 字典中删除对应的 `scene_id` 条目 (`del clients[scene_id]`)。
            - 调用 `await scene_id_manager.release_scene_id(scene_id)` 释放 `scene_id`。**注意**: 如果扫码成功，`/wechat_event` 已经释放过一次 `scene_id`。`SceneIdManager` 的 `release_scene_id` 方法需要能处理重复释放的情况（例如，幂等操作）。
            - 记录客户端连接关闭日志。
      5.  **返回 `EventSourceResponse`**:
          - 使用 `event_generator` 初始化一个 `EventSourceResponse` 对象，并设置 `Access-Control-Allow-Origin: "*"` 头部，然后返回给客户端。这将建立一个持久的 SSE 连接。

**四、整体交互流程 (一个典型的扫码登录/绑定场景)**

1.  **应用启动**: `SceneIdManager` 预创建一批 `scene_id`。
2.  **前端请求二维码**:
    - 前端应用（如网页）向 `GET /sse?token=<valid_token>` 发起请求。
3.  **后端处理 `/sse` 请求**:
    - 验证 `token`。
    - 从 `SceneIdManager` 获取一个 `scene_id` 和 `qrcode_url`。
    - 创建一个 `asyncio.Queue`，并将其与 `scene_id` 关联存储在 `clients` 字典中。
    - 通过 SSE 连接立即向前端发送包含 `scene_id` 和 `qrcode_url` 的初始事件。
    - 前端收到后显示二维码。
    - 后端 `event_generator` 在此 `scene_id` 对应的 `queue` 上等待数据。
4.  **用户扫码**:
    - 用户使用微信扫描前端显示的二维码。
    - 微信服务器向 `POST /wechat_event` 推送扫码事件的 XML 数据，其中包含 `EventKey` (即 `scene_id`) 和用户的 `FromUserName` (OpenID)。
5.  **后端处理 `/wechat_event` 请求**:
    - 解析 XML，提取 `scene_id` 和 `userId`。
    - 通过 `scene_id` 在 `clients` 字典中找到对应的 `asyncio.Queue`。
    - 将 `{"userId": userId, "event": "SCAN/subscribe"}` 放入该队列。
    - 调用 `scene_id_manager.release_scene_id(scene_id)` 释放场景 ID。
    - 向微信服务器返回成功响应。
6.  **后端 `/sse` 推送结果**:
    - 之前在 `/sse` 中等待的 `queue.get()` 收到数据 (用户 OpenID 等信息)。
    - `event_generator` 将此数据作为新的 SSE 事件发送给前端。
7.  **前端接收结果**:
    - 前端通过 SSE 连接收到包含用户 OpenID 的事件。
    - 前端可以根据此 OpenID 执行后续操作（如登录、绑定账户等）。
8.  **连接清理**:
    - SSE 连接在发送完扫码结果后，或者因超时、客户端断开等原因结束后，`event_generator` 中的 `finally` 块会执行，确保从 `clients` 中移除记录并再次尝试释放 `scene_id`。
