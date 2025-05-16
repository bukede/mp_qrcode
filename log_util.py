import logging.config  # 用于从字典配置日志记录器
import os  # 用于路径操作，如创建目录、拼接路径
import re  # 用于正则表达式操作，在 pretty_print 中用于文本格式化
from typing import Any, Callable  # 类型提示，增强代码可读性和健壮性


def replace_value_in_dict(
    dictionary: dict, key_to_replace: str, proc_func: Callable[[Any], Any]
):
    """
    递归地在嵌套字典中查找指定的键，并对其值应用处理函数。
    这个函数的主要目的是动态修改配置字典中的某些值，例如，将相对路径转换为绝对路径。
    通过递归，它可以处理任意深度的嵌套字典结构。

    参数:
        dictionary (dict): 需要处理的字典。
        key_to_replace (str): 需要查找并替换其值的键。
        proc_func (Callable[[Any], Any]): 一个处理函数，接收旧值作为参数，返回新值。
    """
    for key, value in dictionary.items():
        if key == key_to_replace:
            # 如果找到了目标键，则调用处理函数更新其值。
            dictionary[key] = proc_func(value)  # 注意：这里直接修改了传入的字典对象
        elif isinstance(value, dict):
            # 如果值是另一个字典，则递归调用此函数进行处理。
            # 这是实现深度遍历和修改的关键。
            replace_value_in_dict(value, key_to_replace, proc_func)


def load_log_conf(root: str, log_conf: dict[str, Any]):
    """
    加载并应用日志配置。
    主要做了两件事：
    1. 确保日志文件将被写入到项目根目录下的 'logs' 文件夹中。
       它会检查并创建 'logs' 目录（如果不存在），然后修改日志配置字典中
       所有名为 'filename' 的键对应的值，为其添加 'logs/' 前缀。
       这样做是为了集中管理日志文件，并使日志路径配置更灵活。
    2. 使用 `logging.config.dictConfig` 应用修改后的日志配置。

    参数:
        root (str): 项目的根路径。
        log_conf (dict[str, Any]): 从YAML等文件加载的原始日志配置字典。
    """
    # 构建日志目录的绝对路径。
    # 使用 os.path.join 是为了确保路径分隔符在不同操作系统上的兼容性。
    log_dir = os.path.join(root, "logs")
    # 创建日志目录，如果目录已存在则不执行任何操作 (exist_ok=True)。
    # 这是为了确保日志文件有地方存放，避免因目录不存在而写入失败。
    os.makedirs(log_dir, exist_ok=True)

    # 修改日志配置中的 'filename' 字段，使其指向 'logs' 目录。
    # 例如，如果原始配置中 filename 是 'app.log'，处理后会变成 'logs/app.log'。
    # 这个lambda函数定义了如何根据旧的日志文件名生成新的、包含完整路径的文件名。
    replace_value_in_dict(
        log_conf,
        "filename",
        lambda filename_value: os.path.join(log_dir, filename_value),
    )

    # 应用最终的日志配置。
    # dictConfig 是Python标准库 logging.config 中用于从字典配置日志系统的函数。
    logging.config.dictConfig(log_conf)


def pretty_print(message: str):
    """
    将长消息格式化后打印到控制台，使其更易于阅读。
    它会将消息按段落分割，并确保每行不超过一定的字符数（这里是100）。
    这对于打印较长的日志消息或调试信息非常有用，可以避免控制台输出混乱。
    """
    # 1. re.split(r"\n\n+", message): 按一个或多个连续换行符分割消息为段落。
    #    这是为了保留原始消息中的段落结构。
    # 2. for paragraph in ...: 遍历每个段落。
    # 3. paragraph.strip("\n"): 去除段落首尾的换行符。
    # 4. re.findall(r".{1,100}(?:\s+|$)", ...):
    #    - .{1,100}: 匹配1到100个任意字符。
    #    - (?:\s+|$): 非捕获组，匹配一个或多个空白字符，或者匹配行尾。
    #      这确保了在100个字符附近按单词边界或行尾断行，而不是粗暴地截断单词。
    # 5. line.strip(): 去除每行处理后的首尾空白。
    # 6. "\n".join(...): 将处理后的行用单个换行符连接起来，形成格式化后的段落。
    # 7. "\n\n".join(...): 将格式化后的段落用两个换行符连接起来，保持段落间的空行。
    formatted_message = "\n\n".join(
        "\n".join(
            line.strip()
            for line in re.findall(r".{1,100}(?:\s+|$)", paragraph.strip("\n"))
        )
        for paragraph in re.split(r"\n\n+", message)
    )
    print(formatted_message)
