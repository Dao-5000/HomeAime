# -*- coding: utf-8 -*-
"""事件循环兼容层

``asyncio.get_event_loop()`` 已在 Python 3.12 弃用、后续版本会移除：
  · 在协程内调用会给出 DeprecationWarning
  · 在没有运行中循环时行为收紧，容易抛 RuntimeError

项目里有 70+ 处调用，散布在同步与异步两种上下文，逐个人工判断极易出错
（改成 get_running_loop() 后如果那个函数其实是同步的，运行时会直接崩）。

所以这里提供 get_loop()：两种上下文都能安全使用 ——
  · 协程内     → 返回正在运行的循环（等价于 get_running_loop）
  · 同步上下文 → 退回当前线程的循环，没有就新建一个

调用方可以无脑替换，不会因为上下文判断错而 RuntimeError。
"""
import asyncio


def get_loop():
    """返回一个可用的事件循环；任何上下文下都不抛异常。"""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        pass

    # 同步上下文：取当前线程的循环
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
        if loop is not None and not loop.is_closed():
            return loop
    except Exception:
        pass

    # 连线程循环都没有：新建一个并设为当前（老 get_event_loop 的行为）
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop
