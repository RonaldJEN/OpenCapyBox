# OpenSandbox Python SDK 使用手册
 
> 适用版本：`opensandbox` 0.1.4+
> Python 要求：≥ 3.10
> 本手册基于源码整理，所有示例均可直接运行。
 
---
 
## 目录
 
1. [安装与配置](#1-安装与配置)
2. [沙箱生命周期](#2-沙箱生命周期)
3. [执行命令](#3-执行命令)
4. [文件操作](#4-文件操作)
5. [沙箱管理](#5-沙箱管理-sandboxmanager)
6. [高级配置](#6-高级配置)
7. [同步 API](#7-同步-api-sandboxsync)
8. [异常处理](#8-异常处理)
 
---
 
## 1. 安装与配置
 
### 安装
 
```bash
# 从源码安装（本地部署推荐）
cd sdks/sandbox/python
pip install .
 
# 或用 uv
uv pip install .
```
 
### ConnectionConfig 配置
 
`ConnectionConfig` 是所有操作的入口，控制服务器地址、鉴权、超时等。
 
```python
from datetime import timedelta
from opensandbox.config import ConnectionConfig
 
config = ConnectionConfig(
    domain="106.52.22.103:8000",        # 服务器地址（不含协议前缀）
    api_key="ali_sandbox_sk_...",       # API 密钥
    protocol="http",                    # "http" 或 "https"，默认 "http"
    request_timeout=timedelta(seconds=60),  # HTTP 请求超时，默认 30s
    debug=False,                        # 开启后打印 HTTP 请求详情
    use_server_proxy=False,             # 是否通过服务端代理访问沙箱内部
)
```
 
**字段说明：**
 
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `domain` | `str` | `localhost:8080` | 服务器地址，支持 `host:port` 格式 |
| `api_key` | `str` | `None` | 鉴权密钥，也可通过环境变量设置 |
| `protocol` | `str` | `"http"` | 协议，`"http"` 或 `"https"` |
| `request_timeout` | `timedelta` | 30s | 每个 HTTP 请求的超时时间 |
| `debug` | `bool` | `False` | 启用后打印详细 HTTP 日志 |
| `use_server_proxy` | `bool` | `False` | 客户端无法直连沙箱时，通过服务端转发 |
| `headers` | `dict` | `{}` | 追加到每个请求的自定义 HTTP 头 |
| `transport` | `httpx.AsyncBaseTransport` | `None` | 自定义 httpx 传输层（高级用法） |
 
**通过环境变量配置（推荐生产环境）：**
 
```bash
export OPEN_SANDBOX_API_KEY="ali_sandbox_sk_..."
export OPEN_SANDBOX_DOMAIN="106.52.22.103:8000"
```
 
```python
# 不传参数，SDK 自动读取环境变量
config = ConnectionConfig()
```
 
**`domain` 支持的格式：**
 
```python
# 以下三种写法等价
ConnectionConfig(domain="106.52.22.103:8000", protocol="http")
ConnectionConfig(domain="http://106.52.22.103:8000")   # 显式带协议
ConnectionConfig(domain="localhost:8080")               # 默认值
```
 
---
 
## 2. 沙箱生命周期
 
### 2.1 创建沙箱 `Sandbox.create()`
 
```python
import asyncio
from datetime import timedelta
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
 
async def main():
    config = ConnectionConfig(
        domain="106.52.22.103:8000",
        api_key="ali_sandbox_sk_...",
        protocol="http",
        request_timeout=timedelta(seconds=60),
    )
 
    sandbox = await Sandbox.create(
        "code-interpreter-agent:v1.1.0",
        connection_config=config,
        timeout=timedelta(minutes=10),        # 沙箱最大存活时间
        ready_timeout=timedelta(seconds=60),  # 等待就绪的最长时间
        health_check_polling_interval=timedelta(seconds=2),  # 轮询间隔
    )
    print(f"沙箱已就绪，id={sandbox.id}")
 
    await sandbox.kill()
 
asyncio.run(main())
```
 
**create() 全参数：**
 
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `image` | `str \| SandboxImageSpec` | 必填 | 镜像地址 |
| `timeout` | `timedelta` | 10 分钟 | 沙箱最大存活时间 |
| `ready_timeout` | `timedelta` | 30s | 等待健康检查通过的超时 |
| `env` | `dict[str, str]` | `{}` | 环境变量 |
| `metadata` | `dict[str, str]` | `{}` | 自定义标签，用于查询过滤 |
| `resource` | `dict[str, str]` | `{"cpu":"1","memory":"2Gi"}` | 资源限制 |
| `entrypoint` | `list[str]` | `["tail","-f","/dev/null"]` | 容器启动命令 |
| `volumes` | `list[Volume]` | `None` | 挂载卷 |
| `network_policy` | `NetworkPolicy` | `None` | 出口网络策略 |
| `health_check` | `async Callable` | `None` | 自定义健康检查函数 |
| `health_check_polling_interval` | `timedelta` | 200ms | 健康检查轮询间隔 |
| `skip_health_check` | `bool` | `False` | 跳过健康检查，立即返回 |
| `connection_config` | `ConnectionConfig` | `None` | 连接配置 |
 
> **注意：** `skip_health_check=True` 时，沙箱可能尚未完全就绪，需自行等待一段时间再执行命令。
 
### 2.2 连接已有沙箱 `Sandbox.connect()`
 
```python
# 已知 sandbox_id，重新连接（例如程序重启后续用）
sandbox = await Sandbox.connect(
    "24928e3c-cabc-4649-a54d-995fe2968cb6",
    connection_config=config,
    connect_timeout=timedelta(seconds=30),
    skip_health_check=False,
)
```
 
### 2.3 恢复暂停的沙箱 `Sandbox.resume()`
 
```python
# 从 Paused 状态恢复运行
sandbox = await Sandbox.resume(
    sandbox_id,
    connection_config=config,
    resume_timeout=timedelta(seconds=60),
)
```
 
### 2.4 暂停沙箱 `sandbox.pause()`
 
```python
# 暂停：保留所有进程状态，节省资源（可用 resume 恢复）
await sandbox.pause()
```
 
### 2.5 销毁沙箱 `sandbox.kill()`
 
```python
# 不可逆操作，立即终止并销毁沙箱
await sandbox.kill()
```
 
### 2.6 续期 `sandbox.renew()`
 
```python
# 将沙箱有效期延长 30 分钟
response = await sandbox.renew(timedelta(minutes=30))
print(f"新的过期时间：{response.expires_at}")
```
 
### 2.7 使用上下文管理器（推荐）
 
```python
async with await Sandbox.create(image, connection_config=config) as sandbox:
    # 执行操作...
    execution = await sandbox.commands.run("echo hello")
    await sandbox.kill()  # 销毁远端沙箱
# 退出 with 时自动调用 sandbox.close()，释放本地 HTTP 连接
```
 
> **kill() vs close() 区别：**
> - `kill()` — 销毁**远端**容器（不可逆）
> - `close()` — 关闭**本地** HTTP 连接（不影响远端容器）
> - 上下文管理器退出时**只**调用 `close()`，需手动调用 `kill()` 销毁容器
 
### 2.8 查询沙箱信息
 
```python
info = await sandbox.get_info()
print(f"状态: {info.status.state}")    # Running / Paused / Terminated ...
print(f"过期: {info.expires_at}")
print(f"镜像: {info.image.image}")
print(f"元数据: {info.metadata}")
```
 
**SandboxState 状态常量：**
 
| 常量 | 值 | 含义 |
|------|-----|------|
| `SandboxState.PENDING` | `"Pending"` | 正在创建 |
| `SandboxState.RUNNING` | `"Running"` | 运行中，可接受请求 |
| `SandboxState.PAUSING` | `"Pausing"` | 正在暂停 |
| `SandboxState.PAUSED` | `"Paused"` | 已暂停，状态保留 |
| `SandboxState.STOPPING` | `"Stopping"` | 正在终止 |
| `SandboxState.TERMINATED` | `"Terminated"` | 已终止 |
| `SandboxState.FAILED` | `"Failed"` | 发生严重错误 |
 
---
 
## 3. 执行命令
 
### 3.1 基础用法
 
```python
execution = await sandbox.commands.run("echo hello world")
 
# 读取标准输出
for msg in execution.logs.stdout:
    print(msg.text)
 
# 读取标准错误
for msg in execution.logs.stderr:
    print(msg.text, file=sys.stderr)
 
# 检查错误
if execution.error:
    print(f"执行失败: {execution.error.name}: {execution.error.value}")
```
 
### 3.2 RunCommandOpts 选项
 
```python
from opensandbox.models.execd import RunCommandOpts
from datetime import timedelta
 
opts = RunCommandOpts(
    background=False,                       # True = 后台运行（不等待结束）
    working_directory="/workspace",         # 工作目录
    timeout=timedelta(seconds=30),          # 服务端超时（超时后自动终止命令）
)
 
execution = await sandbox.commands.run("python script.py", opts=opts)
```
 
### 3.3 流式输出（实时回调）
 
```python
from opensandbox.models.execd import ExecutionHandlers
 
async def on_stdout(msg):
    print(f"[stdout] {msg.text}", end="")
 
async def on_stderr(msg):
    print(f"[stderr] {msg.text}", end="")
 
async def on_complete(event):
    print(f"\n执行耗时: {event.execution_time_in_millis}ms")
 
handlers = ExecutionHandlers(
    on_stdout=on_stdout,
    on_stderr=on_stderr,
    on_execution_complete=on_complete,
    on_error=lambda e: print(f"错误: {e}"),
)
 
execution = await sandbox.commands.run(
    "for i in $(seq 1 5); do echo $i; sleep 0.5; done",
    handlers=handlers,
)
```
 
**ExecutionHandlers 全部回调：**
 
| 回调 | 触发时机 |
|------|---------|
| `on_stdout` | 收到一行标准输出 |
| `on_stderr` | 收到一行标准错误 |
| `on_result` | 收到执行结果（Jupyter-style） |
| `on_execution_complete` | 命令执行完毕 |
| `on_error` | 发生错误 |
| `on_init` | 执行初始化（获得 execution_id） |
 
### 3.4 后台运行命令
 
```python
import asyncio
 
# 以后台模式启动（立即返回，命令在沙箱内持续运行）
opts = RunCommandOpts(background=True)
execution = await sandbox.commands.run("python long_task.py", opts=opts)
exec_id = execution.id
 
# 轮询状态
while True:
    status = await sandbox.commands.get_command_status(exec_id)
    print(f"运行中: {status.running}, 退出码: {status.exit_code}")
    if not status.running:
        break
    await asyncio.sleep(2)
 
# 获取后台命令的日志
logs = await sandbox.commands.get_background_command_logs(exec_id)
print(logs.content)
 
# 如果需要中止
await sandbox.commands.interrupt(exec_id)
```
 
### 3.5 Execution 返回值结构
 
```python
class Execution:
    id: str | None                    # 执行 ID
    execution_count: int | None       # 执行序号
    result: list[ExecutionResult]     # 执行结果列表（Jupyter-style）
    error: ExecutionError | None      # 错误信息（None 表示成功）
    logs: ExecutionLogs               # 输出日志
 
class ExecutionLogs:
    stdout: list[OutputMessage]       # 标准输出列表
    stderr: list[OutputMessage]       # 标准错误列表
 
class OutputMessage:
    text: str                         # 文本内容
    timestamp: int                    # Unix 毫秒时间戳
    is_error: bool                    # True 时来自 stderr
```
 
---
 
## 4. 文件操作
 
### 4.1 写入文件
 
```python
# 写入文本
await sandbox.files.write_file("/workspace/hello.py", "print('hello')")
 
# 写入二进制
with open("local_file.bin", "rb") as f:
    await sandbox.files.write_file("/workspace/data.bin", f)
 
# 指定权限和所有者
await sandbox.files.write_file(
    "/workspace/script.sh",
    "#!/bin/bash\necho hello",
    mode=0o755,     # 可执行
    owner="root",
    group="root",
)
```
 
### 4.2 批量写入
 
```python
from opensandbox.models.filesystem import WriteEntry
 
entries = [
    WriteEntry(path="/workspace/a.py", data="print('a')"),
    WriteEntry(path="/workspace/b.py", data="print('b')", mode=0o644),
    WriteEntry(path="/workspace/dir/", data=None),  # 只创建目录
]
await sandbox.files.write_files(entries)
```
 
### 4.3 读取文件
 
```python
# 读取文本
content = await sandbox.files.read_file("/workspace/hello.py")
print(content)
 
# 读取二进制
data = await sandbox.files.read_bytes("/workspace/data.bin")
 
# 流式读取大文件（避免内存溢出）
async for chunk in sandbox.files.read_bytes_stream("/workspace/large.bin", chunk_size=64*1024):
    process(chunk)
 
# 读取部分内容（HTTP Range）
partial = await sandbox.files.read_file("/workspace/log.txt", range_header="bytes=0-1023")
```
 
### 4.4 删除文件/目录
 
```python
# 删除文件
await sandbox.files.delete_files(["/workspace/a.py", "/workspace/b.py"])
 
# 删除目录（递归）
await sandbox.files.delete_directories(["/workspace/old_dir"])
```
 
### 4.5 移动/重命名
 
```python
from opensandbox.models.filesystem import MoveEntry
 
await sandbox.files.move_files([
    MoveEntry(src="/workspace/old.py", dest="/workspace/new.py"),
    MoveEntry(src="/tmp/data", dest="/workspace/data"),
])
```
 
### 4.6 搜索文件
 
```python
from opensandbox.models.filesystem import SearchEntry
 
# 搜索 /workspace 下所有 .py 文件
results = await sandbox.files.search(SearchEntry(path="/workspace", pattern="*.py"))
 
for entry in results:
    print(f"{entry.path}  size={entry.size}  modified={entry.modified_at}")
```
 
### 4.7 查询文件信息
 
```python
info_map = await sandbox.files.get_file_info(["/workspace/hello.py", "/workspace"])
for path, info in info_map.items():
    print(f"{path}: size={info.size}, mode={oct(info.mode)}, owner={info.owner}")
```
 
**EntryInfo 字段：**
 
| 字段 | 类型 | 说明 |
|------|------|------|
| `path` | `str` | 文件路径 |
| `mode` | `int` | Unix 权限（如 `0o644`）|
| `owner` | `str` | 所有者用户名 |
| `group` | `str` | 所属组 |
| `size` | `int` | 文件大小（字节），目录为 0 |
| `modified_at` | `datetime` | 最后修改时间 |
| `created_at` | `datetime` | 创建时间 |
 
### 4.8 修改权限
 
```python
from opensandbox.models.filesystem import SetPermissionEntry
 
await sandbox.files.set_permissions([
    SetPermissionEntry(path="/workspace/script.sh", mode=0o755, owner="user"),
])
```
 
### 4.9 内容替换
 
```python
from opensandbox.models.filesystem import ContentReplaceEntry
 
await sandbox.files.replace_contents([
    ContentReplaceEntry(
        path="/workspace/config.py",
        old_content='HOST = "localhost"',
        new_content='HOST = "production.example.com"',
    )
])
```
 
---
 
## 5. 沙箱管理 (SandboxManager)
 
`SandboxManager` 用于批量管理沙箱，不依赖具体某个沙箱实例。
 
### 5.1 创建 Manager
 
```python
from opensandbox import SandboxManager
 
manager = await SandboxManager.create(connection_config=config)
```
 
### 5.2 列出沙箱
 
```python
from opensandbox.models.sandboxes import SandboxFilter
 
# 列出所有运行中的沙箱
result = await manager.list_sandbox_infos(
    SandboxFilter(states=["Running"])
)
 
for info in result.sandbox_infos:
    print(f"{info.id}  状态={info.status.state}  过期={info.expires_at}")
 
# 分页信息
print(f"共 {result.pagination.total_items} 个，第 {result.pagination.page+1} 页")
```
 
**SandboxFilter 参数：**
 
| 参数 | 类型 | 说明 |
|------|------|------|
| `states` | `list[str]` | 按状态过滤，如 `["Running", "Paused"]` |
| `metadata` | `dict[str, str]` | 按 metadata 标签过滤（精确匹配）|
| `page_size` | `int` | 每页数量 |
| `page` | `int` | 页码（从 0 开始）|
 
### 5.3 管理单个沙箱
 
```python
# 查询信息
info = await manager.get_sandbox_info(sandbox_id)
 
# 销毁
await manager.kill_sandbox(sandbox_id)
 
# 续期
response = await manager.renew_sandbox(sandbox_id, timedelta(hours=1))
 
# 暂停
await manager.pause_sandbox(sandbox_id)
 
# 恢复
await manager.resume_sandbox(sandbox_id)
```
 
### 5.4 上下文管理器
 
```python
async with await SandboxManager.create(config) as manager:
    result = await manager.list_sandbox_infos(SandboxFilter(states=["Running"]))
    for info in result.sandbox_infos:
        await manager.kill_sandbox(info.id)
# 退出时自动关闭本地 HTTP 连接
```
 
---
 
## 6. 高级配置
 
### 6.1 资源限制
 
```python
sandbox = await Sandbox.create(
    image,
    resource={
        "cpu": "2",        # CPU 核数
        "memory": "4Gi",   # 内存，支持 Mi/Gi
    },
)
```
 
### 6.2 环境变量注入
 
```python
sandbox = await Sandbox.create(
    image,
    env={
        "PYTHONPATH": "/workspace",
        "DEBUG": "1",
        "DATABASE_URL": "postgresql://...",
    },
)
```
 
### 6.3 自定义标签（用于过滤）
 
```python
sandbox = await Sandbox.create(
    image,
    metadata={
        "project": "my-app",
        "env": "production",
        "user": "alice",
    },
)
 
# 之后可以按标签过滤
result = await manager.list_sandbox_infos(
    SandboxFilter(metadata={"project": "my-app"})
)
```
 
### 6.4 挂载主机目录
 
```python
from opensandbox.models.sandboxes import Volume, Host
 
sandbox = await Sandbox.create(
    image,
    volumes=[
        Volume(
            name="workspace",
            host=Host(path="/data/opensandbox"),  # 宿主机绝对路径
            mount_path="/workspace",              # 容器内挂载点（绝对路径）
            read_only=False,
        )
    ],
)
```
 
> **注意：** `host.path` 必须在 `config.toml` 的 `allowed_host_paths` 中允许。
 
### 6.5 私有镜像鉴权
 
```python
from opensandbox.models.sandboxes import SandboxImageSpec, SandboxImageAuth
 
image_spec = SandboxImageSpec(
    "private.registry.io/my-sandbox:v1.0",
    auth=SandboxImageAuth(
        username="robot$myproject",
        password="your-harbor-token",
    ),
)
 
sandbox = await Sandbox.create(image_spec, connection_config=config)
```
 
### 6.6 出口网络策略
 
```python
from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule
 
# 默认拒绝所有出口流量，只允许访问指定域名
policy = NetworkPolicy(
    default_action="deny",
    egress=[
        NetworkRule(action="allow", target="api.openai.com"),
        NetworkRule(action="allow", target="*.amazonaws.com"),
        NetworkRule(action="deny", target="example-blocked.com"),
    ],
)
 
sandbox = await Sandbox.create(image, network_policy=policy)
```
 
### 6.7 自定义健康检查
 
```python
async def my_health_check(sbx: Sandbox) -> bool:
    """等待沙箱内的 HTTP 服务就绪"""
    try:
        endpoint = await sbx.get_endpoint(8080)
        # 尝试连接服务
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://{endpoint.endpoint}/health", timeout=2)
            return r.status_code == 200
    except Exception:
        return False
 
sandbox = await Sandbox.create(
    "nginx:latest",
    connection_config=config,
    health_check=my_health_check,
    health_check_polling_interval=timedelta(seconds=1),
    ready_timeout=timedelta(seconds=60),
)
```
 
### 6.8 获取沙箱端点
 
```python
# 获取沙箱内指定端口的访问地址
endpoint = await sandbox.get_endpoint(8080)
print(f"访问地址: http://{endpoint.endpoint}")
print(f"需要的请求头: {endpoint.headers}")
```
 
### 6.9 资源监控
 
```python
metrics = await sandbox.get_metrics()
print(f"CPU: {metrics.cpu_used_percentage:.1f}% / {metrics.cpu_count} 核")
print(f"内存: {metrics.memory_used_in_mib:.0f}MiB / {metrics.memory_total_in_mib:.0f}MiB")
```
 
---
 
## 7. 同步 API (SandboxSync)
 
适用于不使用 asyncio 的场景（脚本、Jupyter Notebook、Flask 等）。
 
### 7.1 基础示例
 
```python
from opensandbox import SandboxSync
from opensandbox.config.connection_sync import ConnectionConfigSync
from datetime import timedelta
 
config = ConnectionConfigSync(
    domain="106.52.22.103:8000",
    api_key="ali_sandbox_sk_...",
    protocol="http",
    request_timeout=timedelta(seconds=60),
)
 
# 创建沙箱（阻塞）
sandbox = SandboxSync.create(
    "python:3.11-slim",
    connection_config=config,
    timeout=timedelta(minutes=10),
    skip_health_check=True,
)
 
# 执行命令（阻塞）
execution = sandbox.commands.run("python3 --version")
print(execution.logs.stdout[0].text)
 
# 清理
sandbox.kill()
sandbox.close()
```
 
### 7.2 上下文管理器
 
```python
with SandboxSync.create(image, connection_config=config) as sandbox:
    sandbox.files.write_file("/tmp/test.py", "print('hello')")
    result = sandbox.commands.run("python3 /tmp/test.py")
    print(result.logs.stdout[0].text)
    sandbox.kill()  # 必须手动 kill，with 退出只关闭本地连接
```
 
### 7.3 Async vs Sync 对照表
 
| 操作 | Async | Sync |
|------|-------|------|
| 创建 | `await Sandbox.create(...)` | `SandboxSync.create(...)` |
| 连接 | `await Sandbox.connect(...)` | `SandboxSync.connect(...)` |
| 恢复 | `await Sandbox.resume(...)` | `SandboxSync.resume(...)` |
| 暂停 | `await sandbox.pause()` | `sandbox.pause()` |
| 销毁 | `await sandbox.kill()` | `sandbox.kill()` |
| 续期 | `await sandbox.renew(t)` | `sandbox.renew(t)` |
| 执行命令 | `await sandbox.commands.run(cmd)` | `sandbox.commands.run(cmd)` |
| 写文件 | `await sandbox.files.write_file(...)` | `sandbox.files.write_file(...)` |
| 读文件 | `await sandbox.files.read_file(...)` | `sandbox.files.read_file(...)` |
| 查询信息 | `await sandbox.get_info()` | `sandbox.get_info()` |
| 配置类 | `ConnectionConfig` | `ConnectionConfigSync` |
| 管理器 | `await SandboxManager.create(...)` | `await SandboxManagerSync.create(...)` |
 
> **注意：** 不要在 asyncio 事件循环中调用同步 API（会阻塞事件循环）。
 
---
 
## 8. 异常处理
 
### 8.1 异常类型
 
```python
from opensandbox.exceptions import (
    SandboxException,           # 所有异常的基类
    SandboxApiException,        # HTTP 4xx/5xx 错误
    SandboxInternalException,   # SDK 内部错误
    SandboxUnhealthyException,  # 健康检查失败
    SandboxReadyTimeoutException,  # 等待就绪超时
    InvalidArgumentException,   # 参数错误
)
```
 
| 异常类 | 触发场景 | 常见原因 |
|--------|---------|---------|
| `SandboxApiException` | HTTP 4xx/5xx | 401 未鉴权、404 沙箱不存在、500 服务器错误 |
| `SandboxReadyTimeoutException` | 健康检查超时 | 容器启动慢、`ready_timeout` 太短 |
| `SandboxUnhealthyException` | 健康检查持续失败 | execd 进程未启动、网络不通 |
| `SandboxInternalException` | SDK 内部异常 | 网络断开、序列化错误 |
| `InvalidArgumentException` | 参数非法 | sandbox_id 为空、路径格式错误 |
 
### 8.2 推荐的异常处理模式
 
```python
import asyncio
from datetime import timedelta
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import (
    SandboxReadyTimeoutException,
    SandboxApiException,
    SandboxException,
)
 
async def run_in_sandbox(image: str, command: str) -> str:
    config = ConnectionConfig(
        domain="106.52.22.103:8000",
        api_key="ali_sandbox_sk_...",
        protocol="http",
    )
 
    sandbox = None
    try:
        # 优先正常创建，等待健康检查
        try:
            sandbox = await Sandbox.create(
                image,
                connection_config=config,
                timeout=timedelta(minutes=5),
                ready_timeout=timedelta(seconds=60),
                health_check_polling_interval=timedelta(seconds=2),
            )
        except SandboxReadyTimeoutException:
            # 健康检查超时，尝试跳过（适合本地部署、网络不通的情况）
            print("健康检查超时，改用 skip_health_check 模式")
            sandbox = await Sandbox.create(
                image,
                connection_config=config,
                timeout=timedelta(minutes=5),
                skip_health_check=True,
            )
            # 手动等待一段时间
            await asyncio.sleep(10)
 
        execution = await sandbox.commands.run(
            command,
            opts=RunCommandOpts(timeout=timedelta(seconds=30)),
        )
 
        if execution.error:
            raise RuntimeError(f"命令失败: {execution.error.value}")
 
        return "\n".join(msg.text for msg in execution.logs.stdout)
 
    except SandboxApiException as e:
        print(f"API 错误 (HTTP {e.status_code}): {e}")
        raise
    except SandboxException as e:
        print(f"沙箱错误: {e}")
        raise
    finally:
        if sandbox:
            try:
                await sandbox.kill()
            except Exception:
                pass
            await sandbox.close()
```
 
### 8.3 skip_health_check 使用场景
 
| 场景 | 推荐设置 | 原因 |
|------|---------|------|
| 客户端可直连沙箱容器端口 | `skip_health_check=False`（默认）| 健康检查确保就绪后再返回 |
| 客户端无法直连沙箱（如跨网络）| `skip_health_check=True` | 健康检查会一直 401/连不上 |
| `use_server_proxy=True` | `skip_health_check=False` | 代理模式下健康检查走服务端转发，可以正常通 |
| 容器启动极慢（> 30s）| 增大 `ready_timeout` | 默认 30s 可能不够 |
 
---
 
## 快速参考
 
### 最简可运行示例
 
```python
import asyncio
from datetime import timedelta
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import RunCommandOpts
 
async def main():
    config = ConnectionConfig(
        domain="106.52.22.103:8000",
        api_key="ali_sandbox_sk_...",
        protocol="http",
        request_timeout=timedelta(seconds=60),
    )
 
    sandbox = await Sandbox.create(
        "code-interpreter-agent:v1.1.0",
        connection_config=config,
        timeout=timedelta(minutes=10),
        skip_health_check=True,  # 本地部署无法直连时使用
    )
 
    async with sandbox:
        # 写文件
        await sandbox.files.write_file("/tmp/hello.py", "print('Hello, OpenSandbox!')")
 
        # 执行命令
        result = await sandbox.commands.run(
            "python3 /tmp/hello.py",
            opts=RunCommandOpts(timeout=timedelta(seconds=10)),
        )
        print(result.logs.stdout[0].text)
 
        # 清理
        await sandbox.kill()
 
asyncio.run(main())
```
 
### 导入速查
 
```python
from opensandbox import Sandbox, SandboxManager, SandboxSync, SandboxManagerSync
from opensandbox.config import ConnectionConfig
from opensandbox.config.connection_sync import ConnectionConfigSync
from opensandbox.models.sandboxes import (
    SandboxImageSpec, SandboxImageAuth,
    SandboxFilter, SandboxState,
    NetworkPolicy, NetworkRule,
    Volume, Host, PVC,
)
from opensandbox.models.execd import (
    RunCommandOpts, ExecutionHandlers,
    Execution, ExecutionLogs, OutputMessage,
    CommandStatus, CommandLogs,
)
from opensandbox.models.filesystem import (
    WriteEntry, MoveEntry, SearchEntry,
    SetPermissionEntry, ContentReplaceEntry,
)
from opensandbox.exceptions import (
    SandboxException, SandboxApiException,
    SandboxReadyTimeoutException, SandboxUnhealthyException,
    SandboxInternalException, InvalidArgumentException,
)
```
 