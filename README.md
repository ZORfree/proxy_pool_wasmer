# High-Performance Proxy Pool

高性能、美观的代理池管理系统。支持自动/手动抓取、定时验证、数据库持久化以及强大的管理后台。
全面使用 `aiohttp` 和 `asyncio` 进行异步重构，极大地提升了代理抓取和校验的并发性能。

## ✨ 特性

-   **高性能异步架构**：核心抓取与校验逻辑全面采用 `asyncio` 和 `aiohttp`，支持高并发代理检查，极大地提高了处理速度。
-   **手动触发逻辑**：通过 API 触发抓取与校验，支持后台异步处理，并具备并发执行锁。
-   **双存储引擎**：
    -   **SQLite (默认)**：无需配置，支持 `/data` 卷持久化。
    -   **MySQL (可选)**：通过 `DB_MYSQL=True` 环境变量开启。
-   **高级管理后台**：
    -   **实时仪表盘**：可视化展示协议分布与国家分布。
    -   **代理管理**：支持单选、多选、全选，以及一键批量验证和批量删除。
    -   **动态配置**：无需重启即可修改验证 URL、超时时间、并发数等设置。
-   **多协议支持**：支持 HTTP、HTTPS、SOCKS4 和 SOCKS5 代理。

## 🚀 快速开始

### 1. 本地运行

1.  **安装依赖**:
    ```bash
    pip install -r requirements.txt
    ```
2.  **启动服务**:
    ```bash
    python main.py
    ```
3.  **访问控制台**:
    浏览器打开 `http://localhost:8000`

### 2. Docker 部署 (推荐)

您可以编写 Dockerfile 并挂载 `/data` 目录以持久化 SQLite 数据库：
```bash
docker build -t proxy-pool .
docker run -d -p 8000:8000 -v $(pwd)/data:/data proxy-pool
```

## ⚙️ 环境变量

系统支持通过环境变量进行配置：

| 变量名 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `DB_MYSQL` | 是否启用 MySQL (设置为 `True` 开启，否则使用 SQLite) | `False` |
| `DB_HOST` | MySQL 主机地址 | `127.0.0.1` |
| `DB_PORT` | MySQL 端口 | `3306` |
| `DB_USERNAME` | MySQL 用户名 | `root` |
| `DB_PASSWORD` | MySQL 密码 | - |
| `DB_NAME` | MySQL 数据库名 | `proxy_pool` |

## 🛠️ API 接口说明

所有接口均以 `/api` 开头。

### 代理接口
-   `GET /api/all`: 获取所有代理。支持过滤：`protocol`, `country` (支持多个如 `US,CN`), `max_latency`。
-   `GET /api/random`: 获取一个随机可用代理。参数同上。
-   `GET /api/simple`: 获取纯文本格式（protocol://ip:port）的代理列表，便于与其他工具集成。参数同上。
-   `POST /api/proxy`: 手动添加单个代理。
-   `DELETE /api/proxy`: 删除指定代理（需提供 `ip`, `port`, `protocol`）。
-   `POST /api/batch-delete`: 批量删除代理。请求体：`{"proxies": [{"ip": "...", "port": 0, "protocol": "..."}]}`。

### 任务触发 (异步)
-   `GET /api/fetch`: 触发抓取任务。执行期间会自动加锁防止并发冲突。
-   `GET /api/check`: 触发全量校验。仅校验距离上次检查超过 `validate_interval` 的代理。
-   `POST /api/batch-check`: 针对性批量校验选定代理（不受时间间隔限制）。

### 源与设置
-   `GET /api/sources`: 获取代理源列表。
-   `POST /api/sources`: 添加新的代理源。
-   `DELETE /api/sources/{id}`: 删除指定代理源。
-   `GET /api/settings`: 获取当前系统设置。
-   `PUT /api/settings`: 更新设置（如 `validate_url`, `validate_interval` 等）。

### 系统状态
-   `GET /api/stats`: 获取统计数据（总数、可用数、分布情况）。
-   `GET /api/db-status`: 检查数据库连接状态。

## ⚙️ 系统设置项

可通过 Web 后端或 `PUT /api/settings` 修改：
-   `validate_url`: 用于测试代理可用性的目标 URL (推荐使用 `https://api.ipapi.is`)。
-   `validate_timeout`: 验证超时时间（秒）。
-   `max_concurrency`: 验证时的并发协程数。
-   `validate_interval`: 代理重测间隔（秒）。默认 `600`，即 10 分钟内已检测过的代理不会被 `GET /api/check` 重复检测。

## 📄 许可证

MIT License
