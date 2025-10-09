import dns.resolver
import toml
import time
import platform
import subprocess
import re
import os

CLIENT_NUMBER = 1  # 客户端编号（必填）
DOMAIN = 'frp.test.com'  # 域名(必填)
FRPC_EXE_PATH = ''  # frpc可执行文件路径(默认同目录下的Windows/Linux子目录)
FRPC_CONFIG_PATH = ''  # frpc.toml路径（默认同目录下的Windows/Linux子目录）
CHECK_INTERVAL = 300  # 检查间隔（秒）

frpc_process = None
frpc_connect_port = None

def parse_txt_record(domain):
    try:
        resolver = dns.resolver.Resolver()
        resolver.cache = None  # 禁用缓存
        resolver.nameservers = ['223.5.5.5', '8.8.8.8']
        
        answers = resolver.resolve(domain, 'TXT')
        for rdata in answers:
            for txt_string in rdata.strings:
                txt = txt_string.decode()
                
                # client_local_port 对应 frpc.toml 内的 remotePort
                # client_public_port 为公网连接端口
                local_port_key = f'client_local_port{CLIENT_NUMBER}'
                public_port_key = f'client_public_port{CLIENT_NUMBER}'
                
                # 解析 server_port
                server_match = re.search(r'server_port=(\d+)', txt)
                # 解析 client_local_port (frpc remotePort)
                local_match = re.search(rf'{local_port_key}=(\d+)', txt)
                # 解析 client_public_port (公网连接端口)
                public_match = re.search(rf'{public_port_key}=(\d+)', txt)
                
                if server_match and local_match and public_match:
                    server_port = int(server_match.group(1))
                    remote_port = int(local_match.group(1))
                    public_port = int(public_match.group(1))
                    print(f"[DNS] 成功解析: server_port={server_port}, {local_port_key}={remote_port}, {public_port_key}={public_port}")
                    return server_port, remote_port, public_port
        
        print(f"[WARN] 未找到客户端 {CLIENT_NUMBER} 的配置，请检查 DNS TXT 记录")
    except Exception as e:
        print(f"[ERROR] DNS 查询失败: {e}")
    return None, None, None

def update_frpc_config(server_port, remote_port, public_port):
    global frpc_connect_port
    try:
        config = toml.load(FRPC_CONFIG_PATH)
        old_server = config.get('serverPort')
        old_addr = config.get('serverAddr')

        changed = False
        if old_server != server_port:
            config['serverPort'] = server_port
            changed = True
        if old_addr != DOMAIN:
            config['serverAddr'] = DOMAIN
            changed = True

        # 获取当前代理配置
        old_remote_port = None
        if 'proxies' in config and len(config['proxies']) > 0:
            old_remote_port = config['proxies'][0].get('remotePort')
            # 更新代理远程下发端口（对应 client_local_port）
            if old_remote_port != remote_port:
                config['proxies'][0]['remotePort'] = remote_port
                changed = True
        
        # 更新公网连接端口（对应 client_public_port）
        if frpc_connect_port != public_port:
            frpc_connect_port = public_port # 记录当前公网端口
            changed = True

        if not changed:
            return False  # 无变化

        with open(FRPC_CONFIG_PATH, 'w') as f:
            toml.dump(config, f)

        print(f"[UPDATE] serverAddr={DOMAIN}, serverPort={server_port}, proxies[0].remotePort={remote_port}, 公网端口={public_port}")
        return True
    except Exception as e:
        print(f"更新配置文件失败: {e}")
        return False

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

def start_frpc():
    global frpc_process
    try:
        frpc_process = subprocess.Popen([FRPC_EXE_PATH, '-c', FRPC_CONFIG_PATH], shell=(platform.system() == 'Windows'))
        print("[START] frpc 已启动")
    except Exception as e:
        print(f"启动 frpc 失败: {e}")

def restart_frpc():
    global frpc_process
    try:
        # 检查进程是否存在且正在运行
        if frpc_process:
            if frpc_process.poll() is None:
                print("[RESTART] 正在终止 frpc 进程...")
                frpc_process.terminate()
                try:
                    frpc_process.wait(timeout=5)
                    print("[RESTART] frpc 进程已正常终止")
                except subprocess.TimeoutExpired:
                    print("[RESTART] 进程未响应终止信号，强制结束...")
                    frpc_process.kill()
                    frpc_process.wait(timeout=5)
                    print("[RESTART] frpc 进程已强制结束")
            else:
                print("[RESTART] frpc 进程已不在运行")
            
            # 额外等待一小段时间确保端口释放
            time.sleep(2)
        
        # 启动新进程
        start_frpc()
        print("[RESTART] frpc 已重启")
    except Exception as e:
        print(f"重启 frpc 失败: {e}")

def main():
    print("[START] 启动 frpc 端口自动更新守护进程")
    print(f"[INFO] 客户端编号: {CLIENT_NUMBER}")
    print(f"[INFO] 域名: {DOMAIN}")
    print(f"[INFO] 检查间隔: {CHECK_INTERVAL} 秒")
    
    # 首次启动前先检查并更新配置
    print("\n[INIT] 首次检查 DNS TXT 记录...")
    server_port, remote_port, public_port = parse_txt_record(DOMAIN)
    if server_port:
        update_frpc_config(server_port, remote_port, public_port)

    print(f"[INFO] 连接地址: {DOMAIN}:{frpc_connect_port if frpc_connect_port else '未知'}")
    
    # 启动 frpc
    start_frpc()
    
    # 进入监控循环
    while True:
        try:
            time.sleep(CHECK_INTERVAL)
            print(f"\n[CHECK] 定期检查端口配置...")
            
            server_port, remote_port, public_port = parse_txt_record(DOMAIN)
            if server_port and remote_port and public_port:
                if update_frpc_config(server_port, remote_port, public_port):
                    restart_frpc()
                else:
                    print("[OK] 配置未改变，无需重启")
            else:
                print("[WARN] 未能从 TXT 记录中解析端口，保持当前配置")
        except KeyboardInterrupt:
            print("\n[EXIT] 接收到退出信号...")
            break
        except Exception as e:
            print(f"[ERROR] 主循环异常: {e}")
            time.sleep(60)
    
    # 清理资源
    if frpc_process and frpc_process.poll() is None:
        try:
            frpc_process.terminate()
            frpc_process.wait(timeout=5)
            print("[EXIT] frpc 已停止")
        except:
            pass


if __name__ == '__main__':
    main()
