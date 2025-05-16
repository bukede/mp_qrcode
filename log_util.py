import logging.config
import os
import re
from typing import Any, Callable


def replace_value_in_dict(dictionary, key_to_replace, proc_func: Callable[[Any], Any]):
    for key, value in dictionary.items():
        if key == key_to_replace:
            dictionary[key] = proc_func(dictionary[key])
        elif isinstance(value, dict):
            replace_value_in_dict(value, key_to_replace, proc_func)


def load_log_conf(root: str, log_conf: dict[str, Any]):
    log_dir = os.path.join(root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    replace_value_in_dict(
        log_conf, "filename", lambda value: os.path.join(log_dir, value)
    )
    # print(log_conf)
    logging.config.dictConfig(log_conf)


def pretty_print(message):
    print(
        "\n\n".join(
            "\n".join(
                line.strip()
                for line in re.findall(r".{1,100}(?:\s+|$)", paragraph.strip("\n"))
            )
            for paragraph in re.split(r"\n\n+", message)
        )
    )
