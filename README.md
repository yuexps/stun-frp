# Stun-FRP

基于 STUN 内网穿透的 FRP 工具，无需公网 IP，通过 STUN 打洞技术实现服务端和客户端的自动连接。

## 功能特点

- 🌐 **无需公网 IP** - 基于 STUN 协议，NAT 内网环境也能部署 FRP 服务端
- � **自动打洞映射** - 使用 Natter 进行 STUN 打洞，自动获取公网可访问端口
- 🔄 **智能端口发现** - 自动更新 Cloudflare DNS TXT 记录，客户端自动发现服务器端口
- 🖥️ **跨平台支持** - 支持 Windows 和 Linux 系统
- ⚡ **开箱即用** - 轻量级部署，自动化运行

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

### 服务端部署

1. **配置端口**

编辑 `Stun_Frps/Stun_Port.toml`：

```toml
server_port=7000        # frps 监听端口
client_port1=0          # 客户端1端口（0为自动分配）
client_port2=0          # 客户端2端口
```

2. **配置 Cloudflare**

编辑 `Stun_Frps/Stun_Frps.py`：

```python
DOMAIN = 'frp.test.com'                    # 你的域名
CLOUDFLARE_API_TOKEN = 'your_token_here'   # API Token
CHECK_INTERVAL = 3600                       # 检查间隔(秒)
```

3. **运行服务端**

```bash
# Windows
python Stun_Frps\Stun_Frps.py

# Linux
python3 Stun_Frps/Stun_Frps.py
```

### 客户端部署

1. **配置客户端**

编辑 `Stun_Frpc/Stun_Frpc.py`：

```python
CLIENT_NUMBER = 1                  # 客户端编号（对应 client_port1）
DOMAIN = 'frp.test.com'            # 服务端域名
CHECK_INTERVAL = 300               # DNS 检查间隔(秒)
```

2. **配置 FRP Token**

编辑 `Stun_Frpc/Windows/frpc.toml` 或 `Stun_Frpc/Linux/frpc.toml`，配置你的Token

3. **运行客户端**

```bash
# Windows
python Stun_Frpc\Stun_Frpc.py

# Linux
python3 Stun_Frpc/Stun_Frpc.py
```

## 工作原理

1. **服务端 STUN 打洞** → 即使在 NAT 内网环境，Natter 也能通过 STUN 协议获取公网可访问的端口
2. **DNS 记录发布** → 将打洞获得的公网端口信息写入 Cloudflare DNS TXT 记录
3. **客户端自动发现** → 客户端定期查询 DNS TXT 记录，自动获取服务端的公网端口
4. **动态适应变化** → 当服务端端口变化时，自动更新 DNS 并通知客户端重新连接

**核心优势**：传统 FRP 需要服务端有公网 IP，本工具通过 STUN 打洞技术，让内网服务器也能作为 FRP 服务端运行。

## 依赖项

### Python 依赖

```bash
pip install dnspython toml requests
```

### 外部工具

- [FRP](https://github.com/fatedier/frp) - 内网穿透工具
- [Natter](https://github.com/MikeWang000000/Natter) - STUN 打洞工具

## 注意事项

- 服务端无需公网 IP，但需要网络支持 STUN 协议（大部分运营商 NAT1 环境支持）
- 确保系统防火墙允许相关端口通信
- Cloudflare API Token 需要有 DNS 编辑权限（区域 DNS Token）
- 建议在稳定网络环境下运行，避免因 NAT 映射失效导致频繁重启
- DNS TXT 记录格式：`server_port=7000,client_port1=12345,client_port2=67890 等`
- 如果 NAT 类型过于严格（如对称型 NAT），可能无法成功打洞

## 许可证

本项目仅供学习交流使用。

## 相关链接

- [FRP 官方文档](https://gofrp.org/)
- [Cloudflare API 文档](https://developers.cloudflare.com/api/)
