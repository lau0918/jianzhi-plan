# Render 部署说明（正式线上）

## 前置条件
- 代码已推到 GitHub：`lau0918/jianzhi-plan`
- 已有 `render.yaml` 和 `requirements.txt`

## 部署步骤
1. 登录 Render
2. New -> **Web Service**
3. 选择 GitHub 仓库：`lau0918/jianzhi-plan`
4. 选择 **Use render.yaml**
5. 点击 **Create Web Service**

## 启动配置
Render 会读取 `render.yaml`：
- buildCommand: `pip install -r requirements.txt`
- startCommand: `python3 mobile_server.py --host 0.0.0.0 --port $PORT`

## 访问
部署完成后，Render 会给一个 HTTPS 域名：
```
https://<service-name>.onrender.com
```
手机直接访问该域名即可。

## 常见问题
1. 页面打不开：确认部署日志里没有报错，且服务状态为 `Live`
2. 仍访问本地 IP：确保使用 Render 域名，不要用 `192.168.x.x`
