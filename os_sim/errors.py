# 前端页面功能（OS 存储仿真台）：统一异常类型；前端用 toast(error.message) 展示
# /api/os/action 抛出的错误（如“内容超过一页容量”）。
"""
操作系统子系统错误类型定义。

功能：
- 统一定义页式存储、缓存刷盘、恢复与并发队列中的可识别异常类型，
  便于数据库模块和测试代码按类别处理错误。

对应需求文档：
- 第 4.3 节“页读写（必做）”中对读写错误返回的要求。
- 第 6.2 节“错误返回（必做）”中对接口错误可定位、可区分的要求。
- 第 8 节“测试与验证需求”中对异常处理测试的支撑。
"""

class OSSimError(Exception): pass
class IoError(OSSimError): pass
class InvalidPageId(OSSimError): pass
class CorruptPage(OSSimError): pass
class InvalidPage(InvalidPageId): pass
class InvalidPageDataSize(OSSimError): pass
class CacheAllPinned(OSSimError): pass
class WriteBarrierError(OSSimError): pass
class QueueOverloaded(OSSimError): pass
