import json


def serialize_object(obj, indent=2) -> str:
    """
    对象序列化的入口函数，返回格式化的 JSON 字符串

    Args:
        obj: 任意 Python 对象
        indent: JSON 字符串的缩进量

    Returns:
        格式化的 JSON 字符串
    """

    def _serialize(obj):
        """
        递归序列化对象的内部实现函数
        Args:
            obj: 任意 Python 对象

        Returns:
            序列化后的对象
        """
        # 处理 None
        if obj is None:
            return None

        # 处理基本数据类型
        if isinstance(obj, (str, int, float, bool)):
            return obj

        # 处理列表和元组
        if isinstance(obj, (list, tuple)):
            return [_serialize(item) for item in obj]

        # 处理字典
        if isinstance(obj, dict):
            return {key: _serialize(value) for key, value in obj.items()}

        # 处理set
        if isinstance(obj, set):
            return [_serialize(item) for item in obj]

        # 处理对象
        if hasattr(obj, '__dict__'):
            return _serialize(obj.__dict__)

        # 处理其他类型
        try:
            return str(obj)
        except:
            return None

    # 先进行递归序列化，然后格式化返回
    serialized_data = _serialize(obj)
    return json.dumps(serialized_data, indent=indent, ensure_ascii=False)
