# Wasmer Hello World Demo

这是一个带网页的 FastAPI Demo 项目，专为部署到 **Wasmer Edge** 设计。

## 项目结构

- `main.py`: 后端逻辑 (FastAPI)
- `static/index.html`: 前端界面 (Premium Design)
- `wasmer.toml`: Wasmer 部署配置文件
- `requirements.txt`: 依赖列表

## 如何本地运行

1. 安装依赖:
   ```bash
   pip install -r requirements.txt
   ```
2. 启动应用:
   ```bash
   python main.py
   ```
3. 访问 `http://localhost:8000`

## 如何部署到 Wasmer Edge

1. 安装 Wasmer CLI (如果尚未安装):
   ```bash
   curl https://get.wasmer.io -sSfL | sh
   ```
2. 登录 Wasmer:
   ```bash
   wasmer login
   ```
3. 部署项目:
   ```bash
   wasmer deploy
   ```

部署完成后，你将获得一个类似于 `https://hello-wasmer.wasmer.app` 的访问链接。
