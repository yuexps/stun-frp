import toml
import subprocess
import platform
import os
import sys
import time
import re
import requests
import traceback
import logging
from logging.handlers import RotatingFileHandler

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("未安装 python-dotenv 库，跳过读取 .env 环境变量")
    pass

# 配置项 (从环境变量读取,若未设置则使用默认值)
DOMAIN = os.getenv('STUN_DOMAIN', '')  # Cloudflare托管的域名
CLOUDFLARE_API_TOKEN = os.getenv('CLOUDFLARE_API_TOKEN', '')  # Cloudflare 区域 DNS Token
CHECK_INTERVAL = int(os.getenv('STUN_CHECK_INTERVAL', '300'))  # 定期检查间隔(秒)
FRP_TOKEN = os.getenv('FRP_AUTH_TOKEN', 'stun_frp')  # FRP 认证 Token
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()  # 日志级别
LOG_FILE = os.getenv('LOG_FILE', '')  # 日志文件路径（空表示不写文件）

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


def setup_logger():
    """配置日志系统"""
    logger = logging.getLogger('Stun_Frps')
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    
    # 避免重复添加 handler
    if logger.handlers:
        return logger
    
    # 日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件输出（如果配置了）
    if LOG_FILE:
        try:
            # 确保日志目录存在
            log_dir = os.path.dirname(LOG_FILE)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir)
            
            # 使用 RotatingFileHandler，自动轮转日志
            file_handler = RotatingFileHandler(
                LOG_FILE,
                maxBytes=10*1024*1024,  # 10MB
                backupCount=5,
                encoding='utf-8'
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            logger.info(f"日志文件已配置: {LOG_FILE}")
        except Exception as e:
            logger.warning(f"无法创建日志文件 {LOG_FILE}: {e}")
    
    return logger


# 初始化日志
logger = setup_logger()


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
        if not os.path.exists(STUN_PORT_CONFIG):
            logger.error(f"配置文件不存在: {STUN_PORT_CONFIG}")
            return {}
        
        with open(STUN_PORT_CONFIG, 'r', encoding='utf-8') as f:
            content = f.read().strip().split('\n')
        
        # 解析端口配置
        # 支持格式: 
        # 1. port_name=port_number  (例如: server_port=7000)
        # 2. port_name              (例如: server_port, 自动分配端口)
        port_config = {}
        for line_num, line in enumerate(content, 1):
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
                    if port_number < 0 or port_number > 65535:
                        logger.warning(f"第{line_num}行: 端口号超出范围 (0-65535): {line}")
                        continue
                    port_config[port_name] = port_number
                except ValueError:
                    logger.warning(f"第{line_num}行: 无法解析端口号: {line}")
                    continue
            else:
                # 没有指定端口号，使用 0 (自动分配)
                port_name = line
                if not port_name.replace('_', '').isalnum():
                    logger.warning(f"第{line_num}行: 端口名称包含非法字符: {line}")
                    continue
                port_config[port_name] = 0
        
        if not port_config:
            logger.error("配置文件为空或格式错误")
            return {}
        
        # 检查是否有 server_port
        if 'server_port' not in port_config:
            logger.error("配置文件必须包含 server_port")
            return {}
        
        logger.info(f"读取到 {len(port_config)} 个需要打洞的端口配置: {port_config}")
        return port_config
    except Exception as e:
        logger.error(f"读取 Stun_Port.toml 失败: {e}")
        traceback.print_exc()
        return {}
    

def safe_terminate_process(process, process_name="进程", timeout_terminate=5, timeout_kill=2):
    """
    安全地终止进程，先尝试 terminate，超时后使用 kill
    
    Args:
        process: subprocess.Popen 对象
        process_name: 进程名称（用于日志）
        timeout_terminate: terminate 等待超时时间（秒）
        timeout_kill: kill 等待超时时间（秒）
    
    Returns:
        bool: 是否成功终止
    """
    if not process or process.poll() is not None:
        return True  # 进程已经退出
    
    try:
        logger.info(f"正在终止 {process_name} (PID: {process.pid})...")
        process.terminate()
        try:
            process.wait(timeout=timeout_terminate)
            logger.info(f"{process_name} 已正常终止")
            return True
        except subprocess.TimeoutExpired:
            logger.warning(f"{process_name} 未响应 terminate，使用 kill 强制结束...")
            process.kill()
            try:
                process.wait(timeout=timeout_kill)
                logger.warning(f"{process_name} 已强制结束")
                return True
            except:
                logger.error(f"{process_name} 可能未完全结束")
                return False
    except Exception as e:
        logger.error(f"终止 {process_name} 失败: {e}")
        return False


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
            logger.info(f"{port_name} 第 {retry + 1}/{max_retries} 次尝试打洞...")
            time.sleep(2)  # 重试前等待2秒
        
        try:
            logger.info(f"正在为 {port_name} (本地端口: {local_port if local_port > 0 else '自动分配'}) 启动 natter 打洞...")
            
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
                    logger.error(f"natter 进程异常退出")
                    if remaining_output:
                        logger.debug(f"输出: {remaining_output}")
                    break  # 跳出内层循环,继续重试
                
                line = process.stdout.readline()
                if line:
                    line = line.strip()
                    logger.debug(f"[NATTER] {line}")
                    
                    # 解析映射地址信息
                    # 格式: "tcp://内网IP:内网端口 <--Natter--> tcp://公网IP:公网端口"
                    if '<--Natter-->' in line:
                        match = re.search(r'tcp://([0-9.]+):(\d+)\s+<--Natter-->\s+tcp://([0-9.]+):(\d+)', line)
                        if match:
                            local_ip = match.group(1)
                            actual_local_port = int(match.group(2))
                            public_ip = match.group(3)
                            public_port = int(match.group(4))
                            
                            logger.info(f"{port_name} 打洞成功:")
                            logger.info(f"  - 内网地址: {local_ip}:{actual_local_port}")
                            logger.info(f"  - 公网地址: {public_ip}:{public_port}")
                            
                            return public_ip, public_port, actual_local_port, process
                
                time.sleep(0.1)
            
            # 超时或失败,清理进程后重试
            logger.warning(f"{port_name} 第 {retry + 1} 次打洞超时，未获取到映射地址")
            if process.poll() is None:
                safe_terminate_process(process, f"{port_name} natter", timeout_terminate=5, timeout_kill=2)
                
        except Exception as e:
            logger.error(f"运行 natter 失败 ({port_name}) 第 {retry + 1} 次: {e}")
            traceback.print_exc()
            # 清理可能存在的进程
            try:
                if 'process' in locals() and process:
                    safe_terminate_process(process, f"{port_name} natter", timeout_terminate=3, timeout_kill=1)
            except:
                pass
    
    # 所有重试都失败
    logger.error(f"{port_name} 打洞失败，已重试 {max_retries} 次")
    return None, None, None, None


def update_cloudflare_txt_record(port_mapping):
    """
    更新Cloudflare DNS TXT记录
    port_mapping: dict, 例如 {'server_port': {'local': 7000, 'public': 12345}, 'client_port1': {'local': 7001, 'public': 12346}}
    """
    global zone_id
    
    try:
        if not CLOUDFLARE_API_TOKEN:
            logger.error("Cloudflare API Token 未配置")
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
        logger.info(f"准备更新 TXT 记录: {txt_content}")
        
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
                logger.error(f"未找到域名 {root_domain} 对应的 Zone")
                return False
            
            zone_id = zones[0]['id']
            logger.info(f"缓存 Zone ID: {zone_id}")
        
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
                'ttl': 60
            }
            response = requests.put(update_url, headers=headers, json=data)
        else:
            # 创建新记录
            create_url = f'https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records'
            data = {
                'type': 'TXT',
                'name': DOMAIN,
                'content': txt_content,
                'ttl': 60
            }
            response = requests.post(create_url, headers=headers, json=data)
        
        response.raise_for_status()
        result = response.json()
        
        if result.get('success'):
            logger.info("Cloudflare TXT 记录已更新")
            return True
        else:
            logger.error(f"Cloudflare API 返回错误: {result.get('errors')}")
            return False
            
    except Exception as e:
        logger.error(f"更新 Cloudflare TXT 记录失败: {e}")
        return False


def update_cloudflare_a_record(public_ip):
    """
    更新Cloudflare DNS A记录
    public_ip: 公网IP地址
    """
    global zone_id
    
    try:
        if not CLOUDFLARE_API_TOKEN:
            logger.error("Cloudflare API Token 未配置")
            return False
        
        logger.info(f"准备更新 A 记录: {DOMAIN} -> {public_ip}")
        
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
                logger.error(f"未找到域名 {root_domain} 对应的 Zone")
                return False
            
            zone_id = zones[0]['id']
            logger.info(f"缓存 Zone ID: {zone_id}")
        
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
                'ttl': 60,
                'proxied': False
            }
            response = requests.put(update_url, headers=headers, json=data)
        else:
            # 创建新记录
            create_url = f'https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records'
            data = {
                'type': 'A',
                'name': DOMAIN,
                'content': public_ip,
                'ttl': 60,
                'proxied': False
            }
            response = requests.post(create_url, headers=headers, json=data)
        
        response.raise_for_status()
        result = response.json()
        
        if result.get('success'):
            logger.info(f"Cloudflare A 记录已更新: {DOMAIN} -> {public_ip}")
            return True
        else:
            logger.error(f"Cloudflare API 返回错误: {result.get('errors')}")
            return False
            
    except Exception as e:
        logger.error(f"更新 Cloudflare A 记录失败: {e}")
        return False


def update_frps_config(local_port):
    """
    更新 frps.toml 配置文件中的 bindPort 和 auth.token
    local_port: natter 映射的本地端口(来自 Stun_Port.toml 的 server_port)
    """
    try:
        # 读取 frps.toml
        with open(FRPS_CONFIG_PATH, 'r', encoding='utf-8') as f:
            content = f.read()
            config = toml.loads(content)
        
        changed = False
        
        # 检查并更新 bindPort
        old_bind_port = config.get('bindPort')
        if old_bind_port != local_port:
            config['bindPort'] = local_port
            changed = True
            logger.info(f"frps.toml bindPort: {old_bind_port} -> {local_port}")
        
        # 检查并更新 auth.token (如果环境变量中配置了)
        if FRP_TOKEN:
            if 'auth' not in config:
                config['auth'] = {}
            
            old_token = config['auth'].get('token', '')
            if old_token != FRP_TOKEN:
                config['auth']['method'] = 'token'
                config['auth']['token'] = FRP_TOKEN
                changed = True
                logger.info("frps.toml auth.token 已更新")
        
        if not changed:
            return True  # 无变化
        
        # 写回文件
        with open(FRPS_CONFIG_PATH, 'w', encoding='utf-8') as f:
            content = toml.dumps(config)
            f.write(content)
        
        return True 
        
    except Exception as e:
        logger.error(f"更新 frps.toml 失败: {e}")
        return False


def start_frps():
    """启动frps服务"""
    global frps_process
    try:
        # Windows 上不使用 shell=True，避免子进程无法终止
        # 改为直接使用可执行文件路径
        if platform.system() == 'Windows':
            # Windows: 创建新的进程组，便于终止
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            frps_process = subprocess.Popen(
                [FRPS_EXE_PATH, '-c', FRPS_CONFIG_PATH],
                creationflags=CREATE_NEW_PROCESS_GROUP
            )
        else:
            # Linux/Unix: 直接启动
            frps_process = subprocess.Popen(
                [FRPS_EXE_PATH, '-c', FRPS_CONFIG_PATH]
            )
        logger.info("frps 已启动")
        return True
    except Exception as e:
        logger.error(f"启动 frps 失败: {e}")
        traceback.print_exc()
        frps_process = None
        return False


def restart_frps():
    """重启frps服务"""
    global frps_process
    try:
        if frps_process and frps_process.poll() is None:
            logger.info("正在关闭 frps...")
            if not safe_terminate_process(frps_process, "frps", timeout_terminate=10, timeout_kill=5):
                logger.warning("frps 可能未完全关闭，但仍继续重启流程")
            
            # 等待服务器完全释放所有代理连接
            logger.debug("等待服务器完全关闭并释放资源...")
            time.sleep(3)
        
        # 重置进程对象
        frps_process = None
        
        # 启动新的 frps 进程
        if start_frps():
            logger.info("frps 已重启")
            return True
        else:
            logger.error("frps 重启失败")
            return False
    except Exception as e:
        logger.error(f"重启 frps 失败: {e}")
        frps_process = None
        return False


def perform_stun_and_update():
    """执行STUN打洞并更新配置"""
    global natter_processes
    
    logger.info("\n" + "="*60)
    logger.info("开始执行 STUN 打洞流程")
    logger.info("="*60)
    
    # 1. 读取端口配置
    port_config = read_stun_port_config()
    if not port_config:
        logger.error("未找到需要打洞的端口配置")
        return False
    
    # 2. 为每个端口执行STUN打洞
    port_mapping = {}
    failed_ports = []  # 记录失败的端口
    
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
            logger.error(f"{port_name} 打洞失败，跳过")
            failed_ports.append(port_name)
    
    if not port_mapping:
        logger.error("所有端口打洞均失败")
        return False
    
    # 检查 server_port 是否成功(这是必须的)
    if 'server_port' not in port_mapping:
        logger.error("server_port 打洞失败，无法启动服务")
        return False
    
    if failed_ports:
        logger.warning(f"以下端口打洞失败: {', '.join(failed_ports)}")
    
    logger.info(f"\n端口映射完成: {port_mapping}")
    
    # 3. 更新 frps.toml 配置
    server_local_port = natter_processes['server_port']['local_port']
    if not update_frps_config(server_local_port):
        logger.error("更新 frps 配置失败")
        return False
    
    # 4. 启动/重启 frps 服务
    if frps_process is None or frps_process.poll() is not None:
        if not start_frps():
            logger.error("frps 启动失败")
            return False
        logger.info(f"frps 已启动，监听端口: {server_local_port}")
    else:
        # frps 正在运行,需要重启以应用新配置
        if not restart_frps():
            logger.error("frps 重启失败")
            return False
        logger.info(f"frps 已重启，监听端口: {server_local_port}")
    
    # 5. 更新 Cloudflare DNS 记录
    # 获取 server_port 的公网 IP
    server_public_ip = natter_processes['server_port']['public_ip']
    
    # 更新 A 记录 (域名解析到公网IP)
    update_cloudflare_a_record(server_public_ip)
    
    # 更新 TXT 记录 (端口映射信息)
    update_cloudflare_txt_record(port_mapping)
    
    logger.info("\n" + "="*60)
    logger.info("STUN 打洞流程完成")
    logger.info("="*60 + "\n")
    
    return True


def check_natter_processes():
    """
    检查natter进程是否正常运行，返回异常的端口列表
    注意: natter 使用 -q 参数,当端口映射变化时会自动退出
    
    Returns:
        list: 异常退出的端口名称列表，如果全部正常则返回空列表
    """
    global natter_processes
    
    failed_ports = []
    for port_name, info in list(natter_processes.items()):
        process = info['process']
        returncode = process.poll()
        if returncode is not None:
            logger.warning(f"{port_name} 的 natter 进程已退出 (返回码: {returncode}, 可能是端口映射变化或进程异常)")
            failed_ports.append(port_name)
    
    return failed_ports


def validate_cloudflare_config():
    """验证 Cloudflare 配置是否完整"""
    if not DOMAIN:
        logger.error("未配置 STUN_DOMAIN 环境变量")
        return False
    
    if not CLOUDFLARE_API_TOKEN:
        logger.warning("未配置 CLOUDFLARE_API_TOKEN，将无法更新 DNS 记录")
        return True  # 允许继续运行，只是无法更新DNS
    
    return True


def validate_natter_executable():
    """验证 natter.py 是否存在且可访问"""
    if not os.path.exists(NATTER_PATH):
        logger.error(f"Natter 脚本不存在: {NATTER_PATH}")
        return False
    
    return True


def validate_frps_executable():
    """验证 frps 可执行文件是否存在"""
    if not os.path.exists(FRPS_EXE_PATH):
        logger.error(f"frps 可执行文件不存在: {FRPS_EXE_PATH}")
        return False
    
    if not os.path.exists(FRPS_CONFIG_PATH):
        logger.error(f"frps 配置文件不存在: {FRPS_CONFIG_PATH}")
        return False
    
    return True


def cleanup_natter_processes(port_names=None):
    """
    清理 natter 进程
    
    Args:
        port_names: 要清理的端口名称列表，如果为 None 则清理所有进程
    """
    global natter_processes
    
    if port_names is None:
        # 清理所有进程
        logger.info("清理所有 natter 进程...")
        ports_to_clean = list(natter_processes.keys())
    else:
        # 只清理指定的进程
        logger.info(f"清理指定的 natter 进程: {', '.join(port_names)}")
        ports_to_clean = port_names
    
    for port_name in ports_to_clean:
        if port_name not in natter_processes:
            continue
            
        try:
            info = natter_processes[port_name]
            process = info['process']
            if process.poll() is None:
                safe_terminate_process(process, f"{port_name} natter", timeout_terminate=3, timeout_kill=2)
            else:
                logger.debug(f"{port_name} 的 natter 进程已退出")
            
            # 从字典中移除
            del natter_processes[port_name]
        except Exception as e:
            logger.warning(f"清理 {port_name} 的 natter 进程失败: {e}")
    
    # 如果清理了所有进程，清空字典
    if port_names is None:
        natter_processes.clear()
    
    # 等待端口释放
    if ports_to_clean:
        logger.debug("等待端口完全释放...")
        time.sleep(2)


def restart_single_natter(port_name):
    """
    重启单个 natter 进程
    
    Args:
        port_name: 端口名称
    
    Returns:
        bool: 是否成功重启
    """
    global natter_processes
    
    logger.info(f"准备重启 {port_name} 的 natter 进程...")
    
    # 1. 获取原来的配置
    if port_name in natter_processes:
        old_local_port = natter_processes[port_name]['local_port']
    else:
        # 从配置文件重新读取
        port_config = read_stun_port_config()
        if port_name not in port_config:
            logger.error(f"配置中未找到 {port_name}")
            return False
        old_local_port = port_config[port_name]
    
    # 2. 清理旧进程
    cleanup_natter_processes([port_name])
    
    # 3. 重新打洞
    public_ip, public_port, actual_local_port, process = run_natter_for_port(port_name, old_local_port)
    
    if public_port and process:
        # 4. 更新全局状态
        natter_processes[port_name] = {
            'process': process,
            'public_ip': public_ip,
            'public_port': public_port,
            'local_port': actual_local_port
        }
        
        # 5. 如果是 server_port，需要检查端口是否变化
        if port_name == 'server_port':
            if actual_local_port != old_local_port:
                logger.warning(f"server_port 的本地端口发生变化 ({old_local_port} -> {actual_local_port})")
                logger.info("需要重启 frps 以应用新端口配置...")
                
                # 更新配置
                if not update_frps_config(actual_local_port):
                    logger.error("更新 frps 配置失败")
                    return False
                
                # 重启 frps
                if not restart_frps():
                    logger.error("重启 frps 失败")
                    return False
        
        # 6. 更新 Cloudflare DNS
        # 构造新的端口映射
        port_mapping = {}
        for pname, info in natter_processes.items():
            port_mapping[pname] = {
                'local': info['local_port'],
                'public': info['public_port']
            }
        
        if port_mapping:
            # 更新 A 记录（使用 server_port 的公网 IP）
            if 'server_port' in natter_processes:
                update_cloudflare_a_record(natter_processes['server_port']['public_ip'])
            
            # 更新 TXT 记录
            update_cloudflare_txt_record(port_mapping)
        
        logger.info(f"{port_name} 重启成功")
        return True
    else:
        logger.error(f"{port_name} 重启失败")
        return False


def main():
    """主循环"""
    logger.info("Stun_Frps 服务启动")
    logger.info(f"配置文件: {STUN_PORT_CONFIG}")
    logger.info(f"Natter路径: {NATTER_PATH}")
    logger.info(f"frps路径: {FRPS_EXE_PATH}")
    logger.info(f"域名: {DOMAIN}")
    logger.info(f"检查间隔: {CHECK_INTERVAL} 秒")
    
    # 启动前验证
    logger.info("\n验证配置和文件...")
    if not validate_natter_executable():
        logger.error("Natter 验证失败，程序退出")
        sys.exit(1)
    
    if not validate_frps_executable():
        logger.error("frps 验证失败，程序退出")
        sys.exit(1)
    
    if not validate_cloudflare_config():
        logger.error("Cloudflare 配置验证失败，程序退出")
        sys.exit(1)
    
    logger.info("所有验证通过")
    
    # 初始执行一次
    if not perform_stun_and_update():
        logger.error("初始打洞失败，程序退出")
        sys.exit(1)
    
    # 定期检查
    while True:
        global frps_process  # 声明为全局变量以便修改
        try:
            time.sleep(CHECK_INTERVAL)
            logger.info("\n定期检查 natter 进程状态...")
            
            # 检查进程是否异常 (包括端口变化导致的退出)
            failed_ports = check_natter_processes()
            
            if failed_ports:
                logger.warning(f"检测到 {len(failed_ports)} 个端口异常: {', '.join(failed_ports)}")
                
                # 判断是否需要全量重启
                # 如果 server_port 异常，或者超过一半的端口异常，则全量重启
                total_ports = len(natter_processes) + len(failed_ports)
                need_full_restart = (
                    'server_port' in failed_ports or 
                    len(failed_ports) > total_ports / 2
                )
                
                if need_full_restart:
                    logger.info("关键端口异常或大量端口失败，执行全量重启...")
                    
                    # 先停止 frps 释放端口
                    if frps_process and frps_process.poll() is None:
                        logger.info("停止 frps 以释放端口...")
                        safe_terminate_process(frps_process, "frps", timeout_terminate=5, timeout_kill=2)
                        frps_process = None
                        time.sleep(2)
                    
                    # 清理所有进程
                    cleanup_natter_processes()
                    
                    # 重新打洞
                    if not perform_stun_and_update():
                        logger.warning("全量重启失败，将在下次检查时继续尝试")
                else:
                    logger.info("仅重启异常端口，不影响正常运行的端口...")
                    
                    # 逐个重启失败的端口
                    success_count = 0
                    for port_name in failed_ports:
                        if restart_single_natter(port_name):
                            success_count += 1
                        else:
                            logger.warning(f"{port_name} 重启失败，将在下次检查时继续尝试")
                    
                    logger.info(f"成功重启 {success_count}/{len(failed_ports)} 个端口")
            else:
                logger.info("所有 natter 进程运行正常")
                
        except KeyboardInterrupt:
            logger.info("\n接收到退出信号，正在清理...")
            break
        except Exception as e:
            logger.error(f"主循环异常: {e}")
            traceback.print_exc()
            # 发生异常时也清理一下进程
            cleanup_natter_processes()
            logger.info("等待下次检查...")
            time.sleep(60)
    
    # 清理资源
    logger.info("清理资源...")
    cleanup_natter_processes()
    
    if frps_process and frps_process.poll() is None:
        logger.info("停止 frps 进程...")
        safe_terminate_process(frps_process, "frps", timeout_terminate=5, timeout_kill=2)
    
    logger.info("服务已停止。")


if __name__ == '__main__':
    main()
