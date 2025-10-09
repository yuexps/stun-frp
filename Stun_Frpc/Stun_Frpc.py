import dns.resolver
import toml
import time
import platform
import subprocess
import re
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("未安装 python-dotenv 库，跳过读取 .env 环境变量")
    pass

# 配置项 (从环境变量读取,若未设置则使用默认值)
CLIENT_NUMBERS = [int(x.strip()) for x in os.getenv('STUN_CLIENT_NUMBER', '1').split(',')]  # 客户端编号列表（必填）
DOMAIN = os.getenv('STUN_DOMAIN', '')  # 域名(必填)
CHECK_INTERVAL = int(os.getenv('STUN_CHECK_INTERVAL', '120'))  # 检查间隔(秒)
FRP_TOKEN = os.getenv('FRP_AUTH_TOKEN', 'stun_frp')  # FRP 认证 Token

# frpc可执行文件和配置文件路径（根据操作系统自动选择）
FRPC_EXE_PATH = ''
FRPC_CONFIG_PATH = ''

# 存储每个客户端的进程和端口信息
frpc_processes = {}  # {client_number: process}
frpc_connect_ports = {}  # {client_number: public_port}

def parse_txt_record(domain):
    """解析 DNS TXT 记录，返回所有客户端的配置"""
    try:
        resolver = dns.resolver.Resolver()
        resolver.cache = None  # 禁用缓存
        resolver.nameservers = ['1.1.1.1', '8.8.8.8']
        
        answers = resolver.resolve(domain, 'TXT')
        configs = {}  # {client_number: (server_port, remote_port, public_port)}
        server_port = None
        
        for rdata in answers:
            for txt_string in rdata.strings:
                txt = txt_string.decode()
                
                # 解析 server_port（所有客户端共用）
                if server_port is None:
                    server_match = re.search(r'server_port=(\d+)', txt)
                    if server_match:
                        server_port = int(server_match.group(1))
                
                # 解析每个客户端的配置
                for client_num in CLIENT_NUMBERS:
                    local_port_key = f'client_local_port{client_num}'
                    public_port_key = f'client_public_port{client_num}'
                    
                    # 解析 client_local_port (frpc remotePort)
                    local_match = re.search(rf'{local_port_key}=(\d+)', txt)
                    # 解析 client_public_port (公网连接端口)
                    public_match = re.search(rf'{public_port_key}=(\d+)', txt)
                    
                    if local_match and public_match:
                        remote_port = int(local_match.group(1))
                        public_port = int(public_match.group(1))
                        configs[client_num] = (server_port, remote_port, public_port)
                        print(f"[DNS] 客户端{client_num}: server_port={server_port}, {local_port_key}={remote_port}, {public_port_key}={public_port}")
        
        # 检查是否所有请求的客户端都找到了配置
        for client_num in CLIENT_NUMBERS:
            if client_num not in configs:
                print(f"[WARN] 未找到客户端 {client_num} 的配置，请检查 DNS TXT 记录")
        
        return configs
    except Exception as e:
        print(f"[ERROR] DNS 查询失败: {e}")
        return {}

def update_frpc_config(client_number, server_port, remote_port, public_port):
    """更新指定客户端的 frpc 配置文件"""
    global frpc_connect_ports
    try:
        # 为每个客户端使用独立的配置文件
        base_dir = os.path.dirname(FRPC_CONFIG_PATH)
        config_path = os.path.join(base_dir, f'frpc_{client_number}.toml')
        
        # 如果配置文件不存在，从模板复制
        if not os.path.exists(config_path):
            if os.path.exists(FRPC_CONFIG_PATH):
                import shutil
                shutil.copy(FRPC_CONFIG_PATH, config_path)
                print(f"[INFO] 为客户端{client_number}创建配置文件: {config_path}")
        
        config = toml.load(config_path)
        
        changed = False
        
        # 更新 serverPort
        old_server = config.get('serverPort')
        if old_server != server_port:
            config['serverPort'] = server_port
            changed = True
        
        # 更新 serverAddr
        old_addr = config.get('serverAddr')
        if old_addr != DOMAIN:
            config['serverAddr'] = DOMAIN
            changed = True

        # 更新 auth.token (如果环境变量中配置了)
        if FRP_TOKEN:
            if 'auth' not in config:
                config['auth'] = {}
            
            old_token = config['auth'].get('token', '')
            if old_token != FRP_TOKEN:
                config['auth']['method'] = 'token'
                config['auth']['token'] = FRP_TOKEN
                changed = True
                print(f"[UPDATE] 客户端{client_number} auth.token 已更新")

        # 获取当前代理配置
        old_remote_port = None
        if 'proxies' in config and len(config['proxies']) > 0:
            # 为每个客户端的代理添加唯一名称后缀
            proxy = config['proxies'][0]
            original_name = proxy.get('name', 'proxy')
            # 如果名称还没有客户端编号后缀，添加它
            if not original_name.endswith(f'_client{client_number}'):
                # 移除可能存在的旧后缀
                base_name = re.sub(r'_client\d+$', '', original_name)
                proxy['name'] = f'{base_name}_client{client_number}'
                changed = True
                print(f"[UPDATE] 客户端{client_number}代理名称更新为: {proxy['name']}")
            
            old_remote_port = proxy.get('remotePort')
            # 更新代理远程下发端口（对应 client_local_port）
            if old_remote_port != remote_port:
                proxy['remotePort'] = remote_port
                changed = True
        
        # 更新公网连接端口（对应 client_public_port）
        if frpc_connect_ports.get(client_number) != public_port:
            frpc_connect_ports[client_number] = public_port # 记录当前公网端口
            changed = True

        if not changed:
            return False, config_path  # 无变化

        with open(config_path, 'w') as f:
            toml.dump(config, f)

        print(f"[UPDATE] 客户端{client_number}: serverAddr={DOMAIN}, serverPort={server_port}, remotePort={remote_port}, 公网端口={public_port}")
        return True, config_path
    except Exception as e:
        print(f"更新客户端{client_number}配置文件失败: {e}")
        return False, None

def get_frpc_paths():
    system = platform.system()
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if system == 'Windows':
        exe = os.path.join(base_dir, 'Windows', 'frpc.exe')
        conf = os.path.join(base_dir, 'Windows', 'frpc.toml')
    else:
        exe = os.path.join(base_dir, 'Linux', 'frpc')
        conf = os.path.join(base_dir, 'Linux', 'frpc.toml')
    return exe, conf

FRPC_EXE_PATH, FRPC_CONFIG_PATH = get_frpc_paths()

def start_frpc(client_number, config_path):
    """启动指定客户端的 frpc 进程"""
    global frpc_processes
    try:
        process = subprocess.Popen([FRPC_EXE_PATH, '-c', config_path], shell=(platform.system() == 'Windows'))
        frpc_processes[client_number] = process
        print(f"[START] 客户端{client_number} frpc 已启动")
    except Exception as e:
        print(f"启动客户端{client_number} frpc 失败: {e}")

def restart_frpc(client_number, config_path):
    """重启指定客户端的 frpc 进程"""
    global frpc_processes
    try:
        # 检查进程是否存在且正在运行
        if client_number in frpc_processes:
            process = frpc_processes[client_number]
            if process.poll() is None:
                print(f"[RESTART] 正在终止客户端{client_number} frpc 进程...")
                process.terminate()
                try:
                    process.wait(timeout=5)
                    print(f"[RESTART] 客户端{client_number} frpc 进程已正常终止")
                except subprocess.TimeoutExpired:
                    print(f"[RESTART] 客户端{client_number}进程未响应终止信号，强制结束...")
                    process.kill()
                    process.wait(timeout=5)
                    print(f"[RESTART] 客户端{client_number} frpc 进程已强制结束")
            else:
                print(f"[RESTART] 客户端{client_number} frpc 进程已不在运行")
            
            # 清理进程记录
            del frpc_processes[client_number]
            
            # 等待服务器端完全释放代理连接
            print(f"[RESTART] 等待服务器释放代理连接...")
            time.sleep(3)
        
        # 启动新进程
        start_frpc(client_number, config_path)
        print(f"[RESTART] 客户端{client_number} frpc 已重启")
    except Exception as e:
        print(f"重启客户端{client_number} frpc 失败: {e}")

def main():
    print("[START] 启动 frpc 端口自动更新守护进程")
    print(f"[INFO] 客户端编号: {', '.join(map(str, CLIENT_NUMBERS))}")
    print(f"[INFO] 域名: {DOMAIN}")
    print(f"[INFO] 检查间隔: {CHECK_INTERVAL} 秒")
    
    # 首次启动前先检查并更新配置
    print("\n[INIT] 首次检查 DNS TXT 记录...")
    configs = parse_txt_record(DOMAIN)
    
    # 为每个客户端初始化配置和启动进程
    for client_num in CLIENT_NUMBERS:
        if client_num in configs:
            server_port, remote_port, public_port = configs[client_num]
            changed, config_path = update_frpc_config(client_num, server_port, remote_port, public_port)
            if config_path:
                print(f"[INFO] 客户端{client_num}连接地址: {DOMAIN}:{public_port}")
                start_frpc(client_num, config_path)
        else:
            print(f"[WARN] 跳过客户端{client_num}的启动，未找到配置")
    
    # 进入监控循环
    while True:
        try:
            time.sleep(CHECK_INTERVAL)
            print(f"\n[CHECK] 定期检查端口配置...")
            
            configs = parse_txt_record(DOMAIN)
            
            # 检查每个客户端的配置
            for client_num in CLIENT_NUMBERS:
                if client_num in configs:
                    server_port, remote_port, public_port = configs[client_num]
                    changed, config_path = update_frpc_config(client_num, server_port, remote_port, public_port)
                    if changed and config_path:
                        restart_frpc(client_num, config_path)
                    elif not changed:
                        print(f"[OK] 客户端{client_num}配置未改变，无需重启")
                else:
                    print(f"[WARN] 客户端{client_num}未能从 TXT 记录中解析端口，保持当前配置")
                    
        except KeyboardInterrupt:
            print("\n[EXIT] 接收到退出信号...")
            break
        except Exception as e:
            print(f"[ERROR] 主循环异常: {e}")
            time.sleep(60)
    
    # 清理资源
    for client_num, process in frpc_processes.items():
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
                print(f"[EXIT] 客户端{client_num} frpc 已停止")
            except:
                pass


if __name__ == '__main__':
    main()
