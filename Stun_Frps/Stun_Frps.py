import toml
import subprocess
import platform
import os
import sys
import time
import re
import requests

# 配置项
DOMAIN = 'frp.test.com'  # Cloudflare托管的域名
CLOUDFLARE_API_TOKEN = ''  # Cloudflare 区域 DNS Token https://dash.cloudflare.com/profile/api-tokens
CHECK_INTERVAL = 3600  # 定期检查端口映射是否有效的间隔（秒）

# 路径配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STUN_PORT_CONFIG = os.path.join(BASE_DIR, 'Stun_Port.toml')
NATTER_PATH = os.path.join(BASE_DIR, 'Natter', 'natter.py')

# frps可执行文件和配置文件路径（根据操作系统自动选择）
FRPS_EXE_PATH = ''
FRPS_CONFIG_PATH = ''

# 全局变量
frps_process = None
natter_processes = {}  # 存储每个端口对应的natter进程
zone_id = None  # Cloudflare Zone ID 缓存


def get_frps_paths():
    """根据操作系统获取frps路径"""
    system = platform.system()
    if system == 'Windows':
        exe = os.path.join(BASE_DIR, 'Windows', 'frps.exe')
        conf = os.path.join(BASE_DIR, 'Windows', 'frps.toml')
    else:
        exe = os.path.join(BASE_DIR, 'Linux', 'frps')
        conf = os.path.join(BASE_DIR, 'Linux', 'frps.toml')
    return exe, conf


FRPS_EXE_PATH, FRPS_CONFIG_PATH = get_frps_paths()


def read_stun_port_config():
    """读取Stun_Port.toml配置文件，获取需要打洞的端口配置"""
    try:
        with open(STUN_PORT_CONFIG, 'r', encoding='utf-8') as f:
            content = f.read().strip().split('\n')
        
        # 解析端口配置
        # 支持格式: 
        # 1. port_name=port_number  (例如: server_port=7000)
        # 2. port_name              (例如: server_port, 自动分配端口)
        port_config = {}
        for line in content:
            line = line.strip()
            # 跳过空行和注释
            if not line or line.startswith('#'):
                continue
            
            # 解析格式: name=port 或 name
            if '=' in line:
                parts = line.split('=', 1)
                port_name = parts[0].strip()
                try:
                    port_number = int(parts[1].strip())
                    port_config[port_name] = port_number
                except ValueError:
                    print(f"[WARN] 无法解析端口号: {line}")
                    continue
            else:
                # 没有指定端口号，使用 0 (自动分配)
                port_config[line] = 0
        
        print(f"[CONFIG] 读取到 {len(port_config)} 个需要打洞的端口配置: {port_config}")
        return port_config
    except Exception as e:
        print(f"[ERROR] 读取 Stun_Port.toml 失败: {e}")
        return {}


def run_natter_for_port(port_name, local_port=0, max_retries=3):
    """
    为指定端口运行natter进行STUN打洞
    port_name: 端口名称 (如 server_port)
    local_port: 本地端口号 (如 7000), 0 表示自动分配
    max_retries: 最大重试次数
    返回: (公网IP, 公网端口, 内网端口, natter进程对象)
    """
    for retry in range(max_retries):
        if retry > 0:
            print(f"[RETRY] {port_name} 第 {retry + 1}/{max_retries} 次尝试打洞...")
            time.sleep(2)  # 重试前等待2秒
        
        try:
            print(f"[NATTER] 正在为 {port_name} (本地端口: {local_port if local_port > 0 else '自动分配'}) 启动 natter 打洞...")
            
            # 构造natter命令: python natter.py -q -b <端口>
            # -q 参数: 当映射地址改变时自动退出,便于检测端口变化
            python_cmd = sys.executable
            cmd = [python_cmd, NATTER_PATH, '-q']
            
            # 添加绑定端口参数
            if local_port > 0:
                cmd.extend(['-b', str(local_port)])
            else:
                cmd.extend(['-b', '0'])  # 0表示自动分配端口
            
            # 启动natter进程
            # 合并 stdout 和 stderr,避免遗漏错误信息
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # 合并到 stdout
                text=True,
                bufsize=0,  # 无缓冲,立即输出
                universal_newlines=True
            )
            
            # 等待并解析natter输出获取映射信息
            timeout = 15  # 15秒超时 (给予足够时间建立连接)
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                if process.poll() is not None:
                    # 进程已结束
                    remaining_output = process.stdout.read()
                    print(f"[ERROR] natter 进程异常退出")
                    if remaining_output:
                        print(f"  输出: {remaining_output}")
                    break  # 跳出内层循环,继续重试
                
                line = process.stdout.readline()
                if line:
                    line = line.strip()
                    print(f"[NATTER] {line}")
                    
                    # 解析映射地址信息
                    # 格式: "tcp://内网IP:内网端口 <--Natter--> tcp://公网IP:公网端口"
                    
                    if '<--Natter-->' in line:
                        match = re.search(r'tcp://([0-9.]+):(\d+)\s+<--Natter-->\s+tcp://([0-9.]+):(\d+)', line)
                        if match:
                            local_ip = match.group(1)
                            actual_local_port = int(match.group(2))
                            public_ip = match.group(3)
                            public_port = int(match.group(4))
                            
                            print(f"[SUCCESS] {port_name} 打洞成功:")
                            print(f"  - 内网地址: {local_ip}:{actual_local_port}")
                            print(f"  - 公网地址: {public_ip}:{public_port}")
                            
                            return public_ip, public_port, actual_local_port, process
                
                time.sleep(0.1)
            
            # 超时或失败,清理进程后重试
            print(f"[WARN] {port_name} 第 {retry + 1} 次打洞超时，未获取到映射地址")
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                
        except Exception as e:
            print(f"[ERROR] 运行 natter 失败 ({port_name}) 第 {retry + 1} 次: {e}")
            import traceback
            traceback.print_exc()
    
    # 所有重试都失败
    print(f"[ERROR] {port_name} 打洞失败，已重试 {max_retries} 次")
    return None, None, None, None


def update_cloudflare_txt_record(port_mapping):
    """
    更新Cloudflare DNS TXT记录
    port_mapping: dict, 例如 {'server_port': {'local': 7000, 'public': 12345}, 'client_port1': {'local': 7001, 'public': 12346}}
    """
    global zone_id
    
    try:
        if not CLOUDFLARE_API_TOKEN:
            print("[ERROR] Cloudflare API Token 未配置")
            return False
        
        # 构造TXT记录内容
        # 格式: server_port=public_port, client_local_portX=local_port,client_public_portX=public_port
        txt_parts = []
        for port_name, ports in port_mapping.items():
            if port_name == 'server_port':
                # server_port 记录公网端口
                txt_parts.append(f"{port_name}={ports['public']}")
            else:
                # 其他端口记录本地端口和公网端口
                # 从 client_portX 提取 portX 部分
                port_suffix = port_name.replace('client_', '')
                txt_parts.append(f"client_local_{port_suffix}={ports['local']}")
                txt_parts.append(f"client_public_{port_suffix}={ports['public']}")
        txt_content = '"' + ','.join(txt_parts) + '"'
        print(f"[CLOUDFLARE] 准备更新 TXT 记录: {txt_content}")
        
        headers = {
            'Authorization': f'Bearer {CLOUDFLARE_API_TOKEN}',
            'Content-Type': 'application/json'
        }
        
        # 如果 zone_id 未缓存,则查询
        if not zone_id:
            zone_query_url = 'https://api.cloudflare.com/client/v4/zones'
            # 提取根域名（例如从 frp.stun.msfxp.top 提取 msfxp.top）
            domain_parts = DOMAIN.split('.')
            if len(domain_parts) >= 2:
                root_domain = '.'.join(domain_parts[-2:])
            else:
                root_domain = DOMAIN
            
            zone_params = {'name': root_domain}
            zone_response = requests.get(zone_query_url, headers=headers, params=zone_params)
            zone_response.raise_for_status()
            
            zones = zone_response.json().get('result', [])
            if not zones:
                print(f"[ERROR] 未找到域名 {root_domain} 对应的 Zone")
                return False
            
            zone_id = zones[0]['id']
            print(f"[INFO] 缓存 Zone ID: {zone_id}")
        
        # 查询现有的TXT记录
        list_url = f'https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records'
        params = {'type': 'TXT', 'name': DOMAIN}
        response = requests.get(list_url, headers=headers, params=params)
        response.raise_for_status()
        
        records = response.json().get('result', [])
        
        if records:
            # 更新现有记录
            record_id = records[0]['id']
            update_url = f'https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}'
            data = {
                'type': 'TXT',
                'name': DOMAIN,
                'content': txt_content,
                'ttl': 120  # 2分钟TTL，快速更新
            }
            response = requests.put(update_url, headers=headers, json=data)
        else:
            # 创建新记录
            create_url = f'https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records'
            data = {
                'type': 'TXT',
                'name': DOMAIN,
                'content': txt_content,
                'ttl': 120
            }
            response = requests.post(create_url, headers=headers, json=data)
        
        response.raise_for_status()
        result = response.json()
        
        if result.get('success'):
            print(f"[SUCCESS] Cloudflare TXT 记录已更新")
            return True
        else:
            print(f"[ERROR] Cloudflare API 返回错误: {result.get('errors')}")
            return False
            
    except Exception as e:
        print(f"[ERROR] 更新 Cloudflare TXT 记录失败: {e}")
        return False


def update_cloudflare_a_record(public_ip):
    """
    更新Cloudflare DNS A记录
    public_ip: 公网IP地址
    """
    global zone_id
    
    try:
        if not CLOUDFLARE_API_TOKEN:
            print("[ERROR] Cloudflare API Token 未配置")
            return False
        
        print(f"[CLOUDFLARE] 准备更新 A 记录: {DOMAIN} -> {public_ip}")
        
        headers = {
            'Authorization': f'Bearer {CLOUDFLARE_API_TOKEN}',
            'Content-Type': 'application/json'
        }
        
        # 如果 zone_id 未缓存,则查询
        if not zone_id:
            zone_query_url = 'https://api.cloudflare.com/client/v4/zones'
            domain_parts = DOMAIN.split('.')
            if len(domain_parts) >= 2:
                root_domain = '.'.join(domain_parts[-2:])
            else:
                root_domain = DOMAIN
            
            zone_params = {'name': root_domain}
            zone_response = requests.get(zone_query_url, headers=headers, params=zone_params)
            zone_response.raise_for_status()
            
            zones = zone_response.json().get('result', [])
            if not zones:
                print(f"[ERROR] 未找到域名 {root_domain} 对应的 Zone")
                return False
            
            zone_id = zones[0]['id']
            print(f"[INFO] 缓存 Zone ID: {zone_id}")
        
        # 查询现有的A记录
        list_url = f'https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records'
        params = {'type': 'A', 'name': DOMAIN}
        response = requests.get(list_url, headers=headers, params=params)
        response.raise_for_status()
        
        records = response.json().get('result', [])
        
        if records:
            # 更新现有记录
            record_id = records[0]['id']
            update_url = f'https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}'
            data = {
                'type': 'A',
                'name': DOMAIN,
                'content': public_ip,
                'ttl': 120,  # 2分钟TTL，快速更新
                'proxied': False  # 不使用CDN代理,直接解析到IP
            }
            response = requests.put(update_url, headers=headers, json=data)
        else:
            # 创建新记录
            create_url = f'https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records'
            data = {
                'type': 'A',
                'name': DOMAIN,
                'content': public_ip,
                'ttl': 120,
                'proxied': False
            }
            response = requests.post(create_url, headers=headers, json=data)
        
        response.raise_for_status()
        result = response.json()
        
        if result.get('success'):
            print(f"[SUCCESS] Cloudflare A 记录已更新: {DOMAIN} -> {public_ip}")
            return True
        else:
            print(f"[ERROR] Cloudflare API 返回错误: {result.get('errors')}")
            return False
            
    except Exception as e:
        print(f"[ERROR] 更新 Cloudflare A 记录失败: {e}")
        return False


def update_frps_config(local_port):
    """
    更新 frps.toml 配置文件中的 bindPort
    local_port: natter 映射的本地端口(来自 Stun_Port.toml 的 server_port)
    """
    try:
        # 读取 frps.toml
        with open(FRPS_CONFIG_PATH, 'r', encoding='utf-8') as f:
            content = f.read()
            config = toml.loads(content)
        
        # 检查是否需要更新
        old_bind_port = config.get('bindPort')
        
        if old_bind_port == local_port:
            return True  # 无变化
        
        # 更新 bindPort
        config['bindPort'] = local_port
        
        # 写回文件
        with open(FRPS_CONFIG_PATH, 'w', encoding='utf-8') as f:
            content = toml.dumps(config)
            f.write(content)
        
        print(f"[UPDATE] frps.toml 已更新: bindPort = {old_bind_port} -> {local_port}")
        return True 
        
    except Exception as e:
        print(f"[ERROR] 更新 frps.toml 失败: {e}")
        return False


def start_frps():
    """启动frps服务"""
    global frps_process
    try:
        frps_process = subprocess.Popen(
            [FRPS_EXE_PATH, '-c', FRPS_CONFIG_PATH],
            shell=(platform.system() == 'Windows')
        )
        print("[START] frps 已启动")
    except Exception as e:
        print(f"[ERROR] 启动 frps 失败: {e}")


def restart_frps():
    """重启frps服务"""
    global frps_process
    try:
        if frps_process and frps_process.poll() is None:
            frps_process.terminate()
            frps_process.wait(timeout=10)
            print("[RESTART] frps 已关闭")
        start_frps()
        print("[RESTART] frps 已重启")
    except Exception as e:
        print(f"[ERROR] 重启 frps 失败: {e}")


def perform_stun_and_update():
    """执行STUN打洞并更新配置"""
    global natter_processes
    
    print("\n" + "="*60)
    print("[START] 开始执行 STUN 打洞流程")
    print("="*60)
    
    # 1. 读取端口配置
    port_config = read_stun_port_config()
    if not port_config:
        print("[ERROR] 未找到需要打洞的端口配置")
        return False
    
    # 2. 为每个端口执行STUN打洞
    port_mapping = {}
    
    for port_name, local_port in port_config.items():
        public_ip, public_port, actual_local_port, process = run_natter_for_port(port_name, local_port)
        
        if public_port and process:
            port_mapping[port_name] = {
                'local': actual_local_port,
                'public': public_port
            }
            natter_processes[port_name] = {
                'process': process,
                'public_ip': public_ip,
                'public_port': public_port,
                'local_port': actual_local_port
            }
        else:
            print(f"[ERROR] {port_name} 打洞失败，跳过")
            return False
    
    if not port_mapping:
        print("[ERROR] 所有端口打洞均失败")
        return False
    
    print(f"\n[SUCCESS] 端口映射完成: {port_mapping}")
    
    # 3. 更新 frps.toml 配置
    if 'server_port' in natter_processes:
        server_local_port = natter_processes['server_port']['local_port']
        if not update_frps_config(server_local_port):
            print("[ERROR] 更新 frps 配置失败")
            return False
    else:
        print("[ERROR] 未找到 server_port 的映射")
        return False
    
    # 4. 启动 frps 服务
    if frps_process is None or frps_process.poll() is not None:
        start_frps()
        print(f"[INFO] frps 已启动，监听端口: {server_local_port}")
    
    # 5. 更新 Cloudflare DNS 记录
    # 获取 server_port 的公网 IP
    server_public_ip = natter_processes['server_port']['public_ip']
    
    # 更新 A 记录 (域名解析到公网IP)
    update_cloudflare_a_record(server_public_ip)
    
    # 更新 TXT 记录 (端口映射信息)
    update_cloudflare_txt_record(port_mapping)
    
    print("\n" + "="*60)
    print("[COMPLETE] STUN 打洞流程完成")
    print("="*60 + "\n")
    
    return True


def check_natter_processes():
    """
    检查natter进程是否正常运行，如果异常则重新打洞
    注意: natter 使用 -q 参数,当端口映射变化时会自动退出
    """
    global natter_processes
    
    for port_name, info in list(natter_processes.items()):
        process = info['process']
        if process.poll() is not None:
            print(f"[WARN] {port_name} 的 natter 进程已退出 (可能是端口映射变化或进程异常)")
            return False
    
    return True


def main():
    """主循环"""
    print(f"[INFO] Stun_Frps 服务启动")
    print(f"[INFO] 配置文件: {STUN_PORT_CONFIG}")
    print(f"[INFO] Natter路径: {NATTER_PATH}")
    print(f"[INFO] frps路径: {FRPS_EXE_PATH}")
    print(f"[INFO] 域名: {DOMAIN}")
    
    # 初始执行一次
    if not perform_stun_and_update():
        print("[FATAL] 初始打洞失败，程序退出")
        sys.exit(1)
    
    # 定期检查
    while True:
        try:
            time.sleep(CHECK_INTERVAL)
            print(f"\n[CHECK] 定期检查 natter 进程状态...")
            
            # 检查进程是否异常 (包括端口变化导致的退出)
            processes_ok = check_natter_processes()
            
            if not processes_ok:
                print("[ACTION] 检测到 natter 进程异常或端口变化，重新执行打洞流程")
                
                # 清理现有进程
                for info in natter_processes.values():
                    try:
                        if info['process'].poll() is None:
                            info['process'].terminate()
                    except:
                        pass
                natter_processes.clear()
                
                # 重新打洞
                if not perform_stun_and_update():
                    print("[FATAL] 重新打洞失败，程序退出")
                    break
            else:
                print("[OK] 所有 natter 进程运行正常")
                
        except KeyboardInterrupt:
            print("\n[EXIT] 接收到退出信号，正在清理...")
            break
        except Exception as e:
            print(f"[ERROR] 主循环异常: {e}")
            time.sleep(60)
    
    # 清理资源
    print("[CLEANUP] 清理资源...")
    for info in natter_processes.values():
        try:
            if info['process'].poll() is None:
                info['process'].terminate()
                info['process'].wait(timeout=5)
        except:
            pass
    
    if frps_process and frps_process.poll() is None:
        try:
            frps_process.terminate()
            frps_process.wait(timeout=5)
        except:
            pass
    
    print("[EXIT] Stun_Frps 服务已停止")


if __name__ == '__main__':
    main()
