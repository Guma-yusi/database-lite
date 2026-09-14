# 前端页面功能（OS 存储仿真台）：对应“运行 1000 查询”按钮；server.py 用它提交
# 1000 次并发读页任务，并将 max_depth 等统计回传给前端提示。
"""
有界并发查询队列模块。

功能：
- 使用固定工作线程和有界 FIFO 队列模拟操作系统中的受控并发调度。
- 支持背压、拒绝统计、等待时间统计，用于展示数据库侧批量查询任务如何被受限并发执行。

对应需求文档：
- 第 9 节“可选扩展（简写）”中更细粒度并发控制/多线程安全访问的扩展方向。
- 第 10 节“交付物（建议）”中可运行示例与效果展示。

说明：
- 该文件不属于页式存储与缓存的“必做主线”，而是项目为演示 OS 并发调度思想增加的扩展模块。
"""

from concurrent.futures import Future
from dataclasses import dataclass
from queue import Queue,Full
from threading import Lock,Thread
from time import monotonic
from .errors import QueueOverloaded


@dataclass
class Task:
    sequence:int;submitted_at:float;fn:object;args:tuple;kwargs:dict;future:Future


class QueryScheduler:
    """有界 FIFO 工作队列：固定线程限制并发，队满时阻塞或明确拒绝。"""
    def __init__(self,workers=4,queue_capacity=256):
        self.queue=Queue(queue_capacity);self.lock=Lock();self.sequence=0;self.submitted=self.completed=self.failed=self.rejected=0;self.max_depth=0;self.wait_times=[];self.closed=False
        self.workers=[Thread(target=self._worker,name=f"query-worker-{i}",daemon=True) for i in range(workers)]
        for worker in self.workers:worker.start()
    def submit(self,fn,*args,block=True,timeout=None,**kwargs):
        if self.closed:raise RuntimeError("scheduler is closed")
        with self.lock:self.sequence+=1;seq=self.sequence
        future=Future();task=Task(seq,monotonic(),fn,args,kwargs,future)
        try:self.queue.put(task,block=block,timeout=timeout)
        except Full:
            self.rejected+=1;raise QueueOverloaded("query queue is full; apply backpressure or retry later")
        with self.lock:self.submitted+=1;self.max_depth=max(self.max_depth,self.queue.qsize())
        return future
    def _worker(self):
        while True:
            task=self.queue.get()
            if task is None:self.queue.task_done();return
            self.wait_times.append(monotonic()-task.submitted_at)
            if not task.future.set_running_or_notify_cancel():self.queue.task_done();continue
            try:task.future.set_result(task.fn(*task.args,**task.kwargs));self.completed+=1
            except BaseException as exc:task.future.set_exception(exc);self.failed+=1
            finally:self.queue.task_done()
    def drain(self):self.queue.join()
    def stats(self):
        waits=sorted(self.wait_times);p95=waits[min(len(waits)-1,int(len(waits)*.95))] if waits else 0
        return {"workers":len(self.workers),"capacity":self.queue.maxsize,"depth":self.queue.qsize(),"max_depth":self.max_depth,"submitted":self.submitted,"completed":self.completed,"failed":self.failed,"rejected":self.rejected,"average_wait_ms":(sum(waits)/len(waits)*1000 if waits else 0),"p95_wait_ms":p95*1000}
    def close(self):
        self.drain();self.closed=True
        for _ in self.workers:self.queue.put(None)
        for worker in self.workers:worker.join(timeout=2)
