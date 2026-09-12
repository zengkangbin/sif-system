# SIF 系统服务器部署指南

## 📋 服务器环境要求

- **操作系统**: Ubuntu 20.04+ / CentOS 7+
- **Python**: 3.9+
- **内存**: 至少 2GB RAM
- **磁盘**: 至少 10GB 可用空间
- **域名**: 例如 `sif.yourdomain.com`（用于 HTTPS）

---

## 🚀 部署步骤

### 第一步：准备服务器环境

```bash
# 1. 更新系统
sudo apt update && sudo apt upgrade -y

# 2. 安装必要软件
sudo apt install -y python3.9 python3.9-venv python3-pip nginx supervisor git sqlite3

# 3. 创建部署目录
sudo mkdir -p /opt/sif-system
sudo chown $USER:$USER /opt/sif-system
```

---

### 第二步：上传项目文件

#### 方法A：使用 Git（推荐）

```bash
# 在本地初始化 Git 仓库
cd E:\桌面\SIF综合处理
git init
git add .
git commit -m "Initial deployment"

# 推送到 GitHub/GitLab（需要先创建远程仓库）
git remote add origin https://github.com/your-username/sif-system.git
git push -u origin main

# 在服务器上克隆
cd /opt
git clone https://github.com/your-username/sif-system.git
cd sif-system
```

#### 方法B：使用 SCP 上传

```bash
# 在本地打包（Windows PowerShell）
cd E:\桌面
tar -czf sif-system.tar.gz --exclude='.venv' --exclude='data/*.sqlite3' SIF综合处理/

# 上传到服务器
scp sif-system.tar.gz user@your-server-ip:/tmp/

# 在服务器上解压
ssh user@your-server-ip
cd /opt
tar -xzf /tmp/sif-system.tar.gz
mv SIF综合处理 sif-system
```

---

### 第三步：安装 Python 依赖

```bash
cd /opt/sif-system

# 创建虚拟环境
python3.9 -m venv .venv

# 激活虚拟环境
source .venv/bin/activate

# 升级 pip
pip install --upgrade pip

# 安装依赖
pip install -r requirements.txt
```

---

### 第四步：配置环境变量

编辑 `.env` 文件（服务器版本）：

```bash
cd /opt/sif-system
nano .env
```

**修改以下配置**：

```env
# SIF MCP 配置（保持不变）
SIF_MCP_URL=https://mcp.sif.com/mcp
SIF_MCP_SECRET_KEY=sifmcp260820ef6a94krgnhgab3t

# GPT 配置
OPENAI_API_KEY=你的API密钥
OPENAI_MODEL=gpt-5.5
OPENAI_BASE_URL=https://code1.newcli.com/codex/v1
OPENAI_TIMEOUT_SECONDS=120
OPENAI_MAX_RETRIES=1

# 官方 OpenAI 备用
OPENAI_OFFICIAL_BASE_URL=https://api.openai.com/v1
OPENAI_OFFICIAL_API_KEY=你的官方API密钥
OPENAI_OFFICIAL_MODEL=gpt-5.5

# 服务器配置（重要！）
HOST=0.0.0.0  # 允许外部访问
PORT=8090
DEMO_MODE=false
ANALYSIS_CONCURRENCY=2

# 养号网站集成（修改为服务器地址）
YMX_API_URL=https://ymx.yourdomain.com/api/import_reviews_from_sif.php
YMX_API_KEY=你的生产环境密钥
YMX_ENABLED=true
```

**重要修改点**：
1. `HOST=0.0.0.0` - 允许外部访问
2. `YMX_API_URL` - 改为养号网站的服务器地址
3. `YMX_API_KEY` - 使用更安全的密钥（建议重新生成）

---

### 第五步：初始化数据库

```bash
cd /opt/sif-system

# 创建数据目录
mkdir -p data

# 激活虚拟环境（如果未激活）
source .venv/bin/activate

# 运行一次后端，自动创建数据库
python backend/app.py &
sleep 5
pkill -f "python backend/app.py"

# 检查数据库是否创建成功
ls -lh data/sif.sqlite3
```

**创建管理员账号**：

```bash
cd /opt/sif-system
source .venv/bin/activate
python -c "
from backend.auth import db_connect, hash_password, now_iso
conn = db_connect()
conn.execute(
    'INSERT INTO users (username, password_hash, role, status, created_at) VALUES (?, ?, ?, ?, ?)',
    ('admin', hash_password('your-secure-password'), 'admin', 'active', now_iso())
)
conn.commit()
print('管理员账号创建成功！')
"
```

---

### 第六步：配置 Supervisor（进程守护）

创建 Supervisor 配置文件：

```bash
sudo nano /etc/supervisor/conf.d/sif-system.conf
```

**配置内容**：

```ini
[program:sif-system]
command=/opt/sif-system/.venv/bin/python -m uvicorn backend.app:app --host 0.0.0.0 --port 8090
directory=/opt/sif-system
user=www-data
autostart=true
autorestart=true
redirect_stderr=true
stdout_logfile=/var/log/sif-system.log
stderr_logfile=/var/log/sif-system-error.log
environment=PATH="/opt/sif-system/.venv/bin"
```

**启动服务**：

```bash
# 重新加载 Supervisor 配置
sudo supervisorctl reread
sudo supervisorctl update

# 启动 SIF 系统
sudo supervisorctl start sif-system

# 查看状态
sudo supervisorctl status sif-system

# 查看日志
sudo tail -f /var/log/sif-system.log
```

---

### 第七步：配置 Nginx 反向代理

创建 Nginx 配置文件：

```bash
sudo nano /etc/nginx/sites-available/sif-system
```

**HTTP 配置**（临时测试用）：

```nginx
server {
    listen 80;
    server_name sif.yourdomain.com;  # 改为你的域名
    
    client_max_body_size 20M;  # 允许上传大文件
    
    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # WebSocket 支持（如果需要）
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        
        # 超时设置
        proxy_connect_timeout 600;
        proxy_send_timeout 600;
        proxy_read_timeout 600;
    }
}
```

**启用配置**：

```bash
# 创建符号链接
sudo ln -s /etc/nginx/sites-available/sif-system /etc/nginx/sites-enabled/

# 测试配置
sudo nginx -t

# 重启 Nginx
sudo systemctl restart nginx
```

---

### 第八步：配置 HTTPS（Let's Encrypt）

```bash
# 安装 Certbot
sudo apt install certbot python3-certbot-nginx -y

# 自动配置 HTTPS
sudo certbot --nginx -d sif.yourdomain.com

# 测试自动续期
sudo certbot renew --dry-run
```

Certbot 会自动修改 Nginx 配置，添加 SSL 证书并重定向 HTTP 到 HTTPS。

**最终的 Nginx 配置**（Certbot 自动生成）：

```nginx
server {
    listen 80;
    server_name sif.yourdomain.com;
    return 301 https://$host$request_uri;  # 重定向到 HTTPS
}

server {
    listen 443 ssl http2;
    server_name sif.yourdomain.com;
    
    ssl_certificate /etc/letsencrypt/live/sif.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sif.yourdomain.com/privkey.pem;
    
    client_max_body_size 20M;
    
    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        
        proxy_connect_timeout 600;
        proxy_send_timeout 600;
        proxy_read_timeout 600;
    }
}
```

---

### 第九步：修改前端上传配置

**重要**：需要修改前端 JavaScript 中的养号网站地址。

在服务器上编辑文件：

```bash
cd /opt/sif-system
nano static/app.js
```

**找到这两行**（约第961-962行）：

```javascript
const YMX_BASE_URL = 'http://localhost/ymx.com/api';
const YMX_API_KEY = 'sif-import-key-2026';
```

**修改为服务器地址**：

```javascript
const YMX_BASE_URL = 'https://ymx.yourdomain.com/api';  // 改为养号网站的实际地址
const YMX_API_KEY = '你的生产环境密钥';  // 使用更安全的密钥
```

**重启服务**：

```bash
sudo supervisorctl restart sif-system
```

---

### 第十步：配置防火墙

```bash
# 允许 HTTP 和 HTTPS
sudo ufw allow 'Nginx Full'

# 允许 SSH（重要！否则会断开连接）
sudo ufw allow ssh

# 启用防火墙
sudo ufw enable

# 查看状态
sudo ufw status
```

---

## ✅ 验证部署

### 1. 检查服务状态

```bash
# 检查 Supervisor 服务
sudo supervisorctl status sif-system

# 检查 Nginx 状态
sudo systemctl status nginx

# 检查端口监听
sudo netstat -tlnp | grep 8090
```

### 2. 访问测试

在浏览器中访问：
```
https://sif.yourdomain.com
```

应该能看到 SIF 系统的登录页面。

### 3. 测试登录

使用之前创建的管理员账号登录：
- 用户名：`admin`
- 密码：你设置的密码

---

## 🔧 常用管理命令

```bash
# 查看日志
sudo tail -f /var/log/sif-system.log

# 重启服务
sudo supervisorctl restart sif-system

# 停止服务
sudo supervisorctl stop sif-system

# 启动服务
sudo supervisorctl start sif-system

# 重启 Nginx
sudo systemctl restart nginx

# 查看错误日志
sudo tail -f /var/log/sif-system-error.log
```

---

## 🔐 安全建议

1. **修改默认密钥**：
   - 将 `.env` 中的 `YMX_API_KEY` 改为强密钥
   - 同步修改养号网站 API 文件中的密钥

2. **数据库备份**：
   ```bash
   # 定期备份数据库
   sqlite3 /opt/sif-system/data/sif.sqlite3 ".backup /backup/sif-$(date +%Y%m%d).sqlite3"
   ```

3. **日志轮转**：
   ```bash
   sudo nano /etc/logrotate.d/sif-system
   ```
   
   ```
   /var/log/sif-system*.log {
       daily
       rotate 7
       compress
       delaycompress
       missingok
       notifempty
   }
   ```

4. **限制访问**：
   - 在 Nginx 中添加 IP 白名单（如果只允许特定 IP 访问）

---

## 📝 后续更新

当本地代码修改后，更新服务器：

```bash
# 方法A：使用 Git
cd /opt/sif-system
git pull origin main
sudo supervisorctl restart sif-system

# 方法B：重新上传文件
# 本地打包 → 上传 → 服务器解压 → 重启服务
```

---

## ❓ 常见问题

### 问题1：端口 8090 被占用

```bash
# 查找占用端口的进程
sudo lsof -i :8090

# 杀死进程
sudo kill -9 <PID>
```

### 问题2：权限问题

```bash
# 修复文件权限
sudo chown -R www-data:www-data /opt/sif-system
sudo chmod -R 755 /opt/sif-system
```

### 问题3：数据库锁定

```bash
# 检查是否有其他进程在使用数据库
sudo lsof /opt/sif-system/data/sif.sqlite3
```

---

## 📞 需要帮助？

如果遇到问题，请检查日志：

```bash
# SIF 系统日志
sudo tail -f /var/log/sif-system.log

# Nginx 错误日志
sudo tail -f /var/log/nginx/error.log

# Supervisor 日志
sudo tail -f /var/log/supervisor/supervisord.log
```
