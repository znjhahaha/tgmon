"""tgmon —— TG 爆料频道监控 / 翻译 / 分发。

分两个进程跑：
  tgmon.admin   Web 后台（唯一配置入口），永不连 Telethon
  tgmon.worker  独占 user session，抓取→翻译→去重→出站，并消费任务队列

两者只通过 SQLite 通信（app_setting + task 表）。
"""

__version__ = "0.1.0"
