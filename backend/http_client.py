# -*- coding: utf-8 -*-
"""
httpx.AsyncClient —— 每个事件循环一个实例（loop-local）
铁律：httpx.AsyncClient 的连接池绑定"第一次使用它的事件循环"，跨 loop 复用会永久挂死
（await 永不返回、后台线程池被占满、前端表现为"响应超时"而后端日志零报错）。

背景 bug（2026-08-25 修复）：
  全局单例 AsyncClient 先后被 uvicorn 主循环、后台线程自建的事件循环
  （companion/ai_state/updater.py 等）共用 → 连接池里的 keep-alive 连接
  绑定在别的 loop 上 → 后续请求 await 永久挂起。

方案：client 挂在 loop 对象上（setattr），loop 生命周期结束 client 随之被回收，
主循环 / 各后台线程 loop / asyncio.run 临时 loop 各拿各的 client，绝不共享。
"""
import asyncio
import httpx

# 连接池配置
_LIMITS = httpx.Limits(
    max_connections=20,          # 最多20个并发连接
    max_keepalive_connections=10,  # 保活连接数
    keepalive_expiry=30,         # 保活超时30秒
)

# 统一超时配置
_TIMEOUT = httpx.Timeout(
    connect=5.0,    # 连接超时5秒
    read=120.0,     # 读取超时120秒（glm-5.3-flash 等 reasoning 模型推理慢，30秒会 ReadTimeout 导致复盘/提炼失败）
    write=10.0,     # 写入超时10秒
    pool=5.0,       # 从连接池获取连接超时5秒
)

# 挂到 loop 对象上的属性名
_ATTR = "_ai_companion_httpx_client"


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        limits=_LIMITS,
        timeout=_TIMEOUT,
        follow_redirects=True,
    )


def get_http_client() -> httpx.AsyncClient:
    """获取当前事件循环专属的 httpx client（loop-local，绝不跨 loop 共享）"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        # 同步上下文兜底：一次性 client，不复用不缓存
        return _new_client()
    client = getattr(loop, _ATTR, None)
    if client is None or client.is_closed:
        client = _new_client()
        setattr(loop, _ATTR, client)
    return client


async def close_http_client():
    """关闭当前事件循环的 client（应用关闭时调用）"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    client = getattr(loop, _ATTR, None)
    if client and not client.is_closed:
        try:
            await client.aclose()
        except Exception:
            pass
    setattr(loop, _ATTR, None)
