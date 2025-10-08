import dns.resolver
import toml
import time
import platform
import subprocess
import re
import os

CLIENT_NUMBER = 1  # 客户端编号
DOMAIN = 'frp.test.com'  # 域名
FRPC_EXE_PATH = ''  # frpc可执行文件路径(默认同目录下的Windows/Linux子目录)
FRPC_CONFIG_PATH = ''  # frpc.toml路径（默认同目录下的Windows/Linux子目录）
CHECK_INTERVAL = 300  # 检查间隔（秒）

frpc_process = None

def parse_txt_record(domain):
    try:
        # 创建新的 resolver 避免 DNS 缓存
        resolver = dns.resolver.Resolver()
        resolver.cache = None  # 禁用缓存
        
        answers = resolver.resolve(domain, 'TXT')
        for rdata in answers:
            for txt_string in rdata.strings:
                txt = txt_string.decode()
                
                client_key = f'client_port{CLIENT_NUMBER}'
                match = re.search(rf'server_port=(\d+).*{client_key}=(\d+)', txt)
                if match:
                    server_port = int(match.group(1))
                    remote_port = int(match.group(2))
                    print(f"[DNS] 成功解析: server_port={server_port}, {client_key}={remote_port}")
                    return server_port, remote_port
        
        print(f"[WARN] 未找到 client_port{CLIENT_NUMBER} 的配置，请检查 DNS TXT 记录")
    except Exception as e:
        print(f"[ERROR] DNS 查询失败: {e}")
    return None, None

def update_frpc_config(server_port, remote_port):
    try:
        config = toml.load(FRPC_CONFIG_PATH)
        old_server = config.get('serverPort')
        old_addr = config.get('serverAddr')
        proxies = config.get('proxies', [])

        changed = False
        if old_server != server_port:
            config['serverPort'] = server_port
            changed = True
        if old_addr != DOMAIN:
            config['serverAddr'] = DOMAIN
            changed = True
        if proxies and isinstance(proxies, list):
            if proxies[0].get('remotePort') != remote_port:
                proxies[0]['remotePort'] = remote_port
                changed = True

        if not changed:
            return False  # 无变化

        with open(FRPC_CONFIG_PATH, 'w') as f:
            toml.dump(config, f)

        print(f"[UPDATE] serverAddr={DOMAIN}, serverPort={server_port}, proxies[0].remotePort={remote_port}")
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
        if frpc_process and frpc_process.poll() is None:
            frpc_process.terminate()
            frpc_process.wait(timeout=10)
            print("[RESTART] frpc 已关闭，准备重启")
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
    server_port, remote_port = parse_txt_record(DOMAIN)
    if server_port and remote_port:
        update_frpc_config(server_port, remote_port)
    
    # 启动 frpc
    start_frpc()
    
    # 进入监控循环
    while True:
        try:
            time.sleep(CHECK_INTERVAL)
            print(f"\n[CHECK] 定期检查端口配置...")
            
            server_port, remote_port = parse_txt_record(DOMAIN)
            if server_port and remote_port:
                if update_frpc_config(server_port, remote_port):
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
