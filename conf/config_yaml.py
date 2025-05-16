import logging.config
import os
from typing import Any, Callable

import yaml
from pydantic import BaseModel


def replace_value_in_dict(dictionary, key_to_replace, proc_func: Callable[[any], any]):
    for key, value in dictionary.items():
        if key == key_to_replace:
            dictionary[key] = proc_func(dictionary[key])
        elif isinstance(value, dict):
            replace_value_in_dict(value, key_to_replace, proc_func)


def load_from_yml(root: str, file_name: str, Model: BaseModel):
    try:
        with open(os.path.join(root, "conf", file_name), "rb") as fp:
            data = yaml.safe_load(fp)
            return Model(**data)
    except FileNotFoundError:
        print(f"配置文件 {file_name} 不存在")
        exit(-1)


def conf_log_from_yml(root: str, file_name: str):
    log_conf = load_from_yml(root, file_name, dict[str, Any])
    log_dir = os.path.join(root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    replace_value_in_dict(
        log_conf, "filename", lambda value: os.path.join(log_dir, value)
    )
    logging.config.dictConfig(log_conf)


def save_to_yml(root: str, file_name: str, data: BaseModel):
    try:
        with open(os.path.join(root, "conf", file_name), "w") as file:
            file.write(yaml.dump(BaseModel.model_dump(data), allow_unicode=True))
    except FileNotFoundError:
        print(f"配置文件 {file_name} 不存在")
        exit(-1)
