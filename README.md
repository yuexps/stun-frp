# Stun-FRP

基于 STUN 内网穿透的 FRP 工具，无需公网 IP，通过 STUN 打洞技术实现服务端和客户端的自动连接。


## 工作原理

1. **服务端 STUN 打洞** → 在 NAT1 内网环境，Natter 能通过 STUN 协议获取公网可访问的端口
2. **DNS 记录发布** → 将 STUN 打洞获得的公网端口信息写入 Cloudflare DNS TXT 记录
3. **客户端自动发现** → 客户端定期查询 DNS TXT 记录，自动获取服务端的公网端口
4. **动态适应变化** → 当服务端端口变化时，自动更新 DNS 并通知客户端重新连接


**核心优势**：传统 FRP 需要服务端有公网 IP，本工具通过 STUN 打洞技术，让内网服务器也能作为 FRP 服务端运行。


## 项目结构

```
├── Stun_Frpc/          # FRP 客户端
│   ├── Stun_Frpc.py    # 客户端主程序
│   ├── Windows/        # Windows 版本 frpc
│   └── Linux/          # Linux 版本 frpc
└── Stun_Frps/          # FRP 服务端
    ├── Stun_Frps.py    # 服务端主程序
    ├── Stun_Port.toml  # 端口配置文件
    ├── Natter/         # STUN 打洞工具
    ├── Windows/        # Windows 版本 frps
    └── Linux/          # Linux 版本 frps
```


## 快速开始


### 拉取项目

```bash
git clone https://github.com/yuexps/stun-frp.git
```

### Python 依赖 （Python 3.12+）

**快速安装所有依赖:**
```bash
pip install -r requirements.txt
```

**或手动安装:**
```bash
pip install dnspython toml requests python-dotenv
```

### 服务端部署


1. **配置端口**

编辑 `Stun_Frps/Stun_Port.toml`：

```toml
server_port=7000        # frps 监听端口
client_port1=7001       # 客户端1端口（0为自动分配）
client_port2=0          # 客户端2端口
```


2. **设置环境变量**

创建 `.env` 文件:
```bash
# ====================================
# 服务端 (Stun_Frps) 配置
# ====================================

# Cloudflare托管的域名 (必填)
STUN_DOMAIN=

# Cloudflare API Token (必填)
# 获取地址: https://dash.cloudflare.com/profile/api-tokens
# 权限要求: Zone.DNS (编辑)
CLOUDFLARE_API_TOKEN=

# 定期检查间隔(秒)
STUN_CHECK_INTERVAL=300

# FRP 认证 Token (推荐设置，服务端和客户端必须一致)
FRP_AUTH_TOKEN=
```


3. **运行服务端**

```bash
cd Stun_Frps
python3 Stun_Frps.py
```


### 客户端部署


1. **设置环境变量**

创建 `.env` 文件:
```bash
# ====================================
# 客户端 (Stun_Frpc) 配置
# ====================================

# 客户端编号 (必填，多客户端时设置不同编号: 1, 2, 3...)
STUN_CLIENT_NUMBER=1

# 服务端域名 (必填，与服务端保持一致)
STUN_DOMAIN=

# 定期检查间隔(秒) 默认120秒(2分钟)
STUN_CHECK_INTERVAL=120

# FRP 认证 Token (必须与服务端一致)
FRP_AUTH_TOKEN=
```


3. **运行客户端**

```bash
cd Stun_Frpc
python3 Stun_Frpc.py
```


## 注意事项

- 服务端无需公网 IP，但需要网络支持 STUN 协议（大部分运营商 NAT1 环境支持）
- 确保系统防火墙允许相关端口通信
- Cloudflare API Token 需要有 DNS 编辑权限（区域 DNS Token）
- 建议在稳定网络环境下运行，避免因 NAT 映射失效导致频繁重启
- DNS TXT 记录格式："server_port=12345,client_local_port1=7001,client_public_port1=12346"


## 相关链接

- [FRP](https://github.com/fatedier/frp) - 内网穿透工具
- [Natter](https://github.com/MikeWang000000/Natter) - STUN 打洞工具
- [Cloudflare API 文档](https://developers.cloudflare.com/api/)
