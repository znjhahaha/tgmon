# 主题和扩展

现有 Telegram 采集、QQ 查询与推送、网站、API 和 RSS 共用内容处理链路。
默认不启用外部 MCP 工具，也不包含语音、日报、提醒或主动闲聊。

## 主题

后台「主题与扩展」安装 JSON 主题包，随后在频道编辑页选择。
`gaming` 保留游戏资料与术语，`generic` 不加载游戏知识库。
QQ `/subscribe 通用资讯` 或主题标识可选择主题，原有游戏订阅命令继续有效。

```json
{"key":"science","label":"科技资讯","version":"1","config":{"prompt":"准确翻译科学资讯，保留研究条件与不确定性。","terms":{"NASA":"美国航空航天局"},"aliases":["科技"],"topics":["航天","研究"]}}
```

主题保存在数据库目录下的 `extensions/themes`。`TGMON_EXTENSIONS` 可覆盖路径。
修改主题提示词或术语后，翻译缓存按完整提示词重新计算。

## 本地插件

后台安装服务器上已有的插件目录，插件运行于独立 Python 进程。
进程隔离用于故障隔离，不是操作系统权限沙箱。只安装可信代码。
清单 `plugin.json` 示例：

```json
{"name":"science","version":"1","entrypoint":"main.py","capabilities":["event","source"],"commands":["/science"],"tools":[{"name":"lookup","description":"查询本地科技资料","inputSchema":{"type":"object","properties":{"q":{"type":"string"}},"required":["q"]}}]}
```

协议为 UTF-8 JSON，每行一个对象。日志写 stderr；stdout 只输出协议数据。
请求包含 `id`、`method`、`params`，响应为 `{"id":"原请求ID","result":...}` 或 `error`。
命令返回 `{"text":"回复"}`，工具返回结构化 JSON。
`configure` 通知传入已保存的配置；`event` 通知传入处理事件。通知无需响应。
停用或退出时宿主终止进程；调用超时默认 5 秒，单条协议消息不超过 256 KiB。
后台配置是持久化的，重启后恢复启用状态。修改配置会停用插件，重新启用后生效。

来源插件配置 `sources` 数组，例如：

```json
{"sources":[{"id":"science-feed","title":"科技来源","theme":"science","enabled":true,"translate":true}]}
```

worker 调用 `poll`，参数为 `source`、上次 `cursor`、`limit`。
插件返回 `{"events":[{"source_id":"article-1","text":"原文","deeplink":"https://example.org/1","published_at":"2026-09-09T00:00:00Z","revision":"1"}],"cursor":"下一页位置"}`。
本期来源适配接口接收文本资讯；Telegram 继续负责媒体来源。
页面事件和游标在同一事务保存，事件重放幂等，编辑保留版本。
进程故障不会阻塞其他采集、回复或推送队列。
会话和工具接口参考 AstrBot，不直接兼容其插件包。

## MCP

使用官方 Python SDK `mcp>=1.28,<2`。支持 `stdio` 和 `streamable_http`。
在后台配置服务，再按机器人 AppID 授权具体工具；工具名为 `mcp:服务名:工具名`。
没有授权的机器人不会启动或连接该服务。

```json
[{"name":"local","transport":"stdio","command":"python","args":["/opt/tools/server.py"],"enabled":true,"timeout":8},
 {"name":"remote","transport":"streamable_http","url":"https://example.org/mcp","enabled":false,"timeout":8}]
```

```json
{"机器人AppID":["mcp:local:lookup"]}
```

工具发现缓存 30 秒，调用前再次检查授权与参数 JSON Schema。断连或超时会返回错误，核心任务继续执行。
