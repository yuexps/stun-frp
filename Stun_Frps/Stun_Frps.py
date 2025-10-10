import toml
import subprocess
import platform
import os
import sys
import time
import re
import requests
import logging
import threading
import dns.resolver
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

# 路径配置
# 判断是否为 PyInstaller 打包后的可执行文件
if getattr(sys, 'frozen', False):
    # 如果是打包后的可执行文件，使用可执行文件所在目录
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # 如果是源码运行，使用脚本所在目录
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 日志文件路径（如果是相对路径，则保存在脚本目录下）
LOG_FILE = os.getenv('LOG_FILE', 'stun_frps.log')
if not os.path.isabs(LOG_FILE):
    LOG_FILE = os.path.join(BASE_DIR, LOG_FILE)

STUN_PORT_CONFIG = os.path.join(BASE_DIR, 'Stun_Port.toml')

# Natter 路径：根据是否打包和操作系统选择
if getattr(sys, 'frozen', False):
    # 打包后：使用编译的可执行文件
    if platform.system() == 'Windows':
        NATTER_PATH = os.path.join(BASE_DIR, 'Natter', 'natter.exe')
    else:
        NATTER_PATH = os.path.join(BASE_DIR, 'Natter', 'natter')
else:
    # 源码运行：使用 Python 脚本
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
            
            # 使用 RotatingFileHandler，只保留最新的日志
            file_handler = RotatingFileHandler(
                LOG_FILE,
                maxBytes=10*1024*1024,  # 10MB
                backupCount=0,  # 不保留备份，只保留最新的
                encoding='utf-8'
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            # 显示日志文件的绝对路径
            log_abs_path = os.path.abspath(LOG_FILE)
            logger.info(f"📝 日志文件已配置: {log_abs_path}")
        except Exception as e:
            logger.warning(f"⚠️  无法创建日志文件 {LOG_FILE}: {e}")
    
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
            logger.error(f"❌ 配置文件不存在: {STUN_PORT_CONFIG}")
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
                    if not (0 <= port_number <= 65535):
                        logger.warning(f"⚠️  第{line_num}行: 端口号超出范围 (0-65535): {line}")
                        continue
                    port_config[port_name] = port_number
                except ValueError:
                    logger.warning(f"⚠️  第{line_num}行: 无法解析端口号: {line}")
                    continue
            else:
                # 没有指定端口号，使用 0 (自动分配)
                port_name = line
                if not port_name.replace('_', '').isalnum():
                    logger.warning(f"⚠️  第{line_num}行: 端口名称包含非法字符: {line}")
                    continue
                port_config[port_name] = 0
        
        if not port_config:
            logger.error("❌ 配置文件为空或格式错误")
            return {}
        
        # 检查是否有 server_port
        if 'server_port' not in port_config:
            logger.error("❌ 配置文件必须包含 server_port")
            return {}
        
        logger.info(f"📋 读取到 {len(port_config)} 个端口配置: {', '.join([f'{k}={v}' if v > 0 else f'{k}(自动)' for k, v in port_config.items()])}")
        return port_config
    except Exception as e:
        logger.error(f"❌ 读取 Stun_Port.toml 失败: {e}", exc_info=True)
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
        logger.info(f"🛑 正在终止 {process_name} (PID: {process.pid})...")
        process.terminate()
        try:
            process.wait(timeout=timeout_terminate)
            logger.info(f"✅ {process_name} 已正常终止")
            return True
        except subprocess.TimeoutExpired:
            logger.warning(f"⚠️  {process_name} 未响应 terminate，使用 kill 强制结束...")
            process.kill()
            try:
                process.wait(timeout=timeout_kill)
                logger.warning(f"✅ {process_name} 已强制结束")
                return True
            except:
                logger.error(f"❌ {process_name} 可能未完全结束")
                return False
    except Exception as e:
        logger.error(f"❌ 终止 {process_name} 失败: {e}")
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
            logger.info(f"🔌 正在为 {port_name} (本地端口: {local_port if local_port > 0 else '自动分配'}) 启动 natter 打洞...")
            
            # 构造natter命令
            # 不使用 -q 参数，让 natter 自动处理映射地址变化
            if getattr(sys, 'frozen', False):
                # 打包后：直接运行可执行文件
                cmd = [NATTER_PATH]
            else:
                # 源码运行：使用 Python 解释器运行脚本
                python_cmd = sys.executable
                cmd = [python_cmd, NATTER_PATH]
            
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
                    logger.error(f"❌ natter 进程异常退出")
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
                            
                            logger.info(f"✅ {port_name} 打洞成功")
                            logger.info(f"   ├─ 内网地址: {local_ip}:{actual_local_port}")
                            logger.info(f"   └─ 公网地址: {public_ip}:{public_port}")
                            
                            # 启动后台线程监听 natter 输出，检测映射地址变化
                            monitor_thread = threading.Thread(
                                target=monitor_natter_output,
                                args=(port_name, process),
                                daemon=True,
                                name=f"NatterMonitor-{port_name}"
                            )
                            monitor_thread.start()
                            logger.debug(f"已启动 {port_name} 的 natter 输出监听线程")
                            
                            return public_ip, public_port, actual_local_port, process
                
                time.sleep(0.1)
            
            # 超时或失败,清理进程后重试
            logger.warning(f"⚠️  {port_name} 第 {retry + 1} 次打洞超时，未获取到映射地址")
            if process.poll() is None:
                safe_terminate_process(process, f"{port_name} natter", timeout_terminate=5, timeout_kill=2)
                
        except Exception as e:
            logger.error(f"❌ 运行 natter 失败 ({port_name}) 第 {retry + 1} 次: {e}", exc_info=True)
            # 清理可能存在的进程
            try:
                if 'process' in locals() and process:
                    safe_terminate_process(process, f"{port_name} natter", timeout_terminate=3, timeout_kill=1)
            except:
                pass
    
    # 所有重试都失败
    logger.error(f"❌ {port_name} 打洞失败，已重试 {max_retries} 次")
    return None, None, None, None


def monitor_natter_output(port_name, process):
    """
    监听 natter 进程的输出，检测映射地址变化并更新到内存
    实际的 DNS 更新由定期健康检查负责
    这个函数在后台线程中运行
    
    Args:
        port_name: 端口名称
        process: natter 进程对象
    """
    global natter_processes
    
    try:
        logger.debug(f"开始监听 {port_name} 的 natter 输出...")
        
        while True:
            # 检查进程是否还在运行
            if process.poll() is not None:
                logger.debug(f"{port_name} 的 natter 进程已退出，停止监听")
                break
            
            # 读取输出
            line = process.stdout.readline()
            if not line:
                time.sleep(0.1)
                continue
            
            line = line.strip()
            if not line:
                continue
            
            logger.debug(f"[NATTER-{port_name}] {line}")
            
            # 检测映射地址变化
            # 格式: "tcp://内网IP:内网端口 <--Natter--> tcp://公网IP:公网端口"
            if '<--Natter-->' in line:
                match = re.search(r'tcp://([0-9.]+):(\d+)\s+<--Natter-->\s+tcp://([0-9.]+):(\d+)', line)
                if match:
                    local_ip = match.group(1)
                    actual_local_port = int(match.group(2))
                    new_public_ip = match.group(3)
                    new_public_port = int(match.group(4))
                    
                    # 检查内存中的记录是否需要更新
                    if port_name in natter_processes:
                        old_public_port = natter_processes[port_name]['public_port']
                        old_public_ip = natter_processes[port_name]['public_ip']
                        
                        if old_public_port != new_public_port or old_public_ip != new_public_ip:
                            logger.info(f"ℹ️  {port_name} 检测到映射地址变化:")
                            logger.info(f"   ├─ 旧地址: {old_public_ip}:{old_public_port}")
                            logger.info(f"   └─ 新地址: {new_public_ip}:{new_public_port}")
                            
                            # 仅更新内存中的记录
                            natter_processes[port_name]['public_ip'] = new_public_ip
                            natter_processes[port_name]['public_port'] = new_public_port
                            natter_processes[port_name]['local_port'] = actual_local_port
                            
                            logger.info(f"✅ {port_name} 内存记录已更新，等待定期检查同步到 DNS")
                        else:
                            logger.debug(f"{port_name} 映射地址无变化")
                    
    except Exception as e:
        logger.error(f"❌ 监听 {port_name} natter 输出失败: {e}", exc_info=True)


def get_current_dns_txt_record():
    """
    通过 DNS 查询获取当前 TXT 记录并解析端口映射
    
    Returns:
        dict: {port_name: {'local': local_port, 'public': public_port}}
        None: 查询失败
        {}: 记录为空
    """
    try:
        # 配置 DNS 解析器
        resolver = dns.resolver.Resolver()
        resolver.cache = None  # 禁用缓存，获取最新记录
        resolver.nameservers = ['1.1.1.1', '8.8.8.8']  # 使用 Cloudflare 和 Google DNS
        resolver.timeout = 5  # 5秒超时
        resolver.lifetime = 10  # 总生存时间10秒
        
        # 查询 TXT 记录
        answers = resolver.resolve(DOMAIN, 'TXT')
        
        if not answers:
            logger.debug("DNS TXT 记录为空")
            return {}
        
        # 解析 TXT 记录内容
        port_mapping = {}
        
        for rdata in answers:
            for txt_string in rdata.strings:
                txt_content = txt_string.decode()
                logger.debug(f"DNS TXT 记录: {txt_content}")
                
                # 解析 server_port
                server_match = re.search(r'server_port=(\d+)', txt_content)
                if server_match:
                    port_mapping['server_port'] = {
                        'local': 0,  # server_port 不记录 local
                        'public': int(server_match.group(1))
                    }
                
                # 解析 client_portX
                # 查找所有 client_local_portX 和 client_public_portX
                local_ports = re.findall(r'client_local_(port\d+)=(\d+)', txt_content)
                public_ports = re.findall(r'client_public_(port\d+)=(\d+)', txt_content)
                
                # 构建字典
                local_dict = {port: int(value) for port, value in local_ports}
                public_dict = {port: int(value) for port, value in public_ports}
                
                # 合并
                for port_suffix in set(local_dict.keys()) | set(public_dict.keys()):
                    port_name = f'client_{port_suffix}'
                    port_mapping[port_name] = {
                        'local': local_dict.get(port_suffix, 0),
                        'public': public_dict.get(port_suffix, 0)
                    }
        
        return port_mapping
        
    except dns.resolver.NXDOMAIN:
        logger.warning(f"⚠️  域名 {DOMAIN} 不存在")
        return None
    except dns.resolver.NoAnswer:
        logger.debug(f"域名 {DOMAIN} 没有 TXT 记录")
        return {}
    except dns.resolver.Timeout:
        logger.warning(f"⚠️  DNS 查询超时")
        return None
    except Exception as e:
        logger.error(f"❌ DNS 查询失败: {e}")
        return None


def get_zone_id():
    """
    获取 Cloudflare Zone ID（带缓存）
    
    Returns:
        str: Zone ID
        None: 获取失败
    """
    global zone_id
    
    if zone_id:
        return zone_id
    
    try:
        headers = {
            'Authorization': f'Bearer {CLOUDFLARE_API_TOKEN}',
            'Content-Type': 'application/json'
        }
        
        # 提取根域名
        domain_parts = DOMAIN.split('.')
        if len(domain_parts) >= 2:
            root_domain = '.'.join(domain_parts[-2:])
        else:
            root_domain = DOMAIN
        
        # 查询 Zone ID
        zone_query_url = 'https://api.cloudflare.com/client/v4/zones'
        zone_params = {'name': root_domain}
        zone_response = requests.get(zone_query_url, headers=headers, params=zone_params, timeout=10)
        zone_response.raise_for_status()
        
        zones = zone_response.json().get('result', [])
        if not zones:
            logger.error(f"未找到域名 {root_domain} 对应的 Zone")
            return None
        
        zone_id = zones[0]['id']
        logger.info(f"✅ 获取 Zone ID: {zone_id}")
        return zone_id
        
    except Exception as e:
        logger.error(f"❌ 获取 Zone ID 失败: {e}")
        return None


def update_cloudflare_txt_record(port_mapping):
    """
    更新Cloudflare DNS TXT记录
    port_mapping: dict, 例如 {'server_port': {'local': 7000, 'public': 12345}, 'client_port1': {'local': 7001, 'public': 12346}}
    """
    try:
        if not CLOUDFLARE_API_TOKEN:
            logger.error("❌ Cloudflare API Token 未配置")
            return False
        
        # 获取 Zone ID
        current_zone_id = get_zone_id()
        if not current_zone_id:
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
        logger.info(f"📝 准备更新 TXT 记录: {txt_content}")
        
        headers = {
            'Authorization': f'Bearer {CLOUDFLARE_API_TOKEN}',
            'Content-Type': 'application/json'
        }
        
        # 查询现有的TXT记录
        list_url = f'https://api.cloudflare.com/client/v4/zones/{current_zone_id}/dns_records'
        params = {'type': 'TXT', 'name': DOMAIN}
        response = requests.get(list_url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        
        records = response.json().get('result', [])
        
        # 更新或创建记录
        data = {
            'type': 'TXT',
            'name': DOMAIN,
            'content': txt_content,
            'ttl': 60
        }
        
        if records:
            # 更新现有记录
            record_id = records[0]['id']
            update_url = f'https://api.cloudflare.com/client/v4/zones/{current_zone_id}/dns_records/{record_id}'
            response = requests.put(update_url, headers=headers, json=data, timeout=10)
        else:
            # 创建新记录
            create_url = f'https://api.cloudflare.com/client/v4/zones/{current_zone_id}/dns_records'
            response = requests.post(create_url, headers=headers, json=data, timeout=10)
        
        response.raise_for_status()
        result = response.json()
        
        if result.get('success'):
            logger.info("✅ Cloudflare TXT 记录已更新")
            return True
        else:
            logger.error(f"❌ Cloudflare API 返回错误: {result.get('errors')}")
            return False
            
    except Exception as e:
        logger.error(f"❌ 更新 Cloudflare TXT 记录失败: {e}")
        return False


def update_cloudflare_a_record(public_ip):
    """
    更新Cloudflare DNS A记录
    public_ip: 公网IP地址
    """
    try:
        if not CLOUDFLARE_API_TOKEN:
            logger.error("❌ Cloudflare API Token 未配置")
            return False
        
        # 获取 Zone ID
        current_zone_id = get_zone_id()
        if not current_zone_id:
            return False
        
        logger.info(f"📝 准备更新 A 记录: {DOMAIN} -> {public_ip}")
        
        headers = {
            'Authorization': f'Bearer {CLOUDFLARE_API_TOKEN}',
            'Content-Type': 'application/json'
        }
        
        # 查询现有的A记录
        list_url = f'https://api.cloudflare.com/client/v4/zones/{current_zone_id}/dns_records'
        params = {'type': 'A', 'name': DOMAIN}
        response = requests.get(list_url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        
        records = response.json().get('result', [])
        
        # 更新或创建记录
        data = {
            'type': 'A',
            'name': DOMAIN,
            'content': public_ip,
            'ttl': 60,
            'proxied': False
        }
        
        if records:
            # 更新现有记录
            record_id = records[0]['id']
            update_url = f'https://api.cloudflare.com/client/v4/zones/{current_zone_id}/dns_records/{record_id}'
            response = requests.put(update_url, headers=headers, json=data, timeout=10)
        else:
            # 创建新记录
            create_url = f'https://api.cloudflare.com/client/v4/zones/{current_zone_id}/dns_records'
            response = requests.post(create_url, headers=headers, json=data, timeout=10)
        
        response.raise_for_status()
        result = response.json()
        
        if result.get('success'):
            logger.info(f"✅ Cloudflare A 记录已更新: {DOMAIN} -> {public_ip}")
            return True
        else:
            logger.error(f"❌ Cloudflare API 返回错误: {result.get('errors')}")
            return False
            
    except Exception as e:
        logger.error(f"❌ 更新 Cloudflare A 记录失败: {e}")
        return False


def update_frps_config(local_port):
    """
    更新 frps.toml 配置文件中的 bindPort 和 auth.token
    local_port: natter 映射的本地端口(来自 Stun_Port.toml 的 server_port)
    """
    try:
        # 读取 frps.toml
        with open(FRPS_CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = toml.load(f)
        
        changed = False
        
        # 检查并更新 bindPort
        old_bind_port = config.get('bindPort')
        if old_bind_port != local_port:
            config['bindPort'] = local_port
            changed = True
            logger.info(f"⚙️  frps.toml bindPort: {old_bind_port} -> {local_port}")
        
        # 检查并更新 auth.token (如果环境变量中配置了)
        if FRP_TOKEN:
            if 'auth' not in config:
                config['auth'] = {}
            
            old_token = config['auth'].get('token', '')
            if old_token != FRP_TOKEN:
                config['auth']['method'] = 'token'
                config['auth']['token'] = FRP_TOKEN
                changed = True
                logger.info("⚙️  frps.toml auth.token 已更新")
        
        if not changed:
            return True  # 无变化
        
        # 写回文件
        with open(FRPS_CONFIG_PATH, 'w', encoding='utf-8') as f:
            toml.dump(config, f)
        
        return True 
        
    except Exception as e:
        logger.error(f"❌ 更新 frps.toml 失败: {e}")
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
        logger.info("✅ frps 已启动")
        return True
    except Exception as e:
        logger.error(f"启动 frps 失败: {e}", exc_info=True)
        frps_process = None
        return False


def restart_frps():
    """重启frps服务"""
    global frps_process
    try:
        if frps_process and frps_process.poll() is None:
            logger.info("🛑 正在关闭 frps...")
            if not safe_terminate_process(frps_process, "frps", timeout_terminate=10, timeout_kill=5):
                logger.warning("⚠️  frps 可能未完全关闭，但仍继续重启流程")
            
            # 等待服务器完全释放所有代理连接
            logger.debug("等待服务器完全关闭并释放资源...")
            time.sleep(3)
        
        # 重置进程对象
        frps_process = None
        
        # 启动新的 frps 进程
        if start_frps():
            logger.info("✅ frps 已重启")
            return True
        else:
            logger.error("❌ frps 重启失败")
            return False
    except Exception as e:
        logger.error(f"❌ 重启 frps 失败: {e}")
        frps_process = None
        return False


def perform_stun_and_update():
    """执行STUN打洞并更新配置"""
    global natter_processes
    
    logger.info("")
    logger.info("="*70)
    logger.info("🚀 开始执行 STUN 打洞流程")
    logger.info("="*70)
    
    # 1. 读取端口配置
    port_config = read_stun_port_config()
    if not port_config:
        logger.error("❌ 未找到需要打洞的端口配置")
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
            logger.error(f"❌ {port_name} 打洞失败，跳过")
            failed_ports.append(port_name)
    
    if not port_mapping:
        logger.error("❌ 所有端口打洞均失败")
        return False
    
    # 检查 server_port 是否成功(这是必须的)
    if 'server_port' not in port_mapping:
        logger.error("❌ server_port 打洞失败，无法启动服务")
        return False
    
    if failed_ports:
        logger.warning(f"⚠️  以下端口打洞失败: {', '.join(failed_ports)}")
    
    logger.info(f"✅ 端口映射完成 ({len(port_mapping)}/{len(port_config)} 成功)")
    
    # 3. 更新 frps.toml 配置
    server_local_port = natter_processes['server_port']['local_port']
    if not update_frps_config(server_local_port):
        logger.error("❌ 更新 frps 配置失败")
        return False
    
    # 4. 启动/重启 frps 服务
    if frps_process is None or frps_process.poll() is not None:
        if not start_frps():
            logger.error("❌ frps 启动失败")
            return False
        logger.info(f"✅ frps 已启动，监听端口: {server_local_port}")
    else:
        # frps 正在运行,需要重启以应用新配置
        if not restart_frps():
            logger.error("❌ frps 重启失败")
            return False
        logger.info(f"✅ frps 已重启，监听端口: {server_local_port}")
    
    # 5. 更新 Cloudflare DNS 记录
    # 获取 server_port 的公网 IP
    server_public_ip = natter_processes['server_port']['public_ip']
    
    # 更新 A 记录 (域名解析到公网IP)
    update_cloudflare_a_record(server_public_ip)
    
    # 更新 TXT 记录 (端口映射信息)
    update_cloudflare_txt_record(port_mapping)
    
    logger.info("")
    logger.info("="*70)
    logger.info("🎉 STUN 打洞流程完成")
    logger.info("="*70)
    logger.info("")
    
    return True


def check_natter_processes():
    """
    检查 natter 进程是否正常运行，并对比内存与 DNS 记录
    natter 会自动处理映射地址变化，只需检查进程是否存活
    同时负责将内存中的映射信息同步到 DNS
    
    Returns:
        list: 异常退出的端口名称列表，如果全部正常则返回空列表
    """
    global natter_processes
    
    failed_ports = []
    
    # 1. 检查进程健康状态
    for port_name, info in list(natter_processes.items()):
        process = info['process']
        returncode = process.poll()
        if returncode is not None:
            # 进程已退出，说明发生异常
            logger.warning(f"⚠️  {port_name} 的 natter 进程异常退出 (返回码: {returncode})")
            failed_ports.append(port_name)
    
    # 2. 对比内存与 DNS，同步映射信息
    if not failed_ports:  # 只有在没有异常进程时才进行同步检查
        try:
            logger.debug("🔍 检查内存与 DNS 记录是否一致...")
            
            # 查询当前 DNS 记录
            current_dns = get_current_dns_txt_record()
            
            if current_dns is None:
                logger.warning("⚠️  无法查询 DNS 记录，跳过本次同步检查")
                return failed_ports
            
            # 构建内存中的端口映射
            memory_mapping = {
                pname: {
                    'local': info['local_port'],
                    'public': info['public_port']
                }
                for pname, info in natter_processes.items()
                if info['process'].poll() is None  # 只包含运行中的进程
            }
            
            # 对比内存与 DNS
            needs_update = False
            changes = []
            
            for port_name, memory_ports in memory_mapping.items():
                dns_public_port = None
                if port_name in current_dns:
                    dns_public_port = current_dns[port_name]['public']
                
                if dns_public_port != memory_ports['public']:
                    needs_update = True
                    changes.append(f"{port_name}: DNS={dns_public_port or '无'} → 内存={memory_ports['public']}")
            
            # 检查 DNS 中是否有内存中不存在的端口（可能是进程已退出但 DNS 未清理）
            for port_name in current_dns:
                if port_name not in memory_mapping:
                    needs_update = True
                    changes.append(f"{port_name}: DNS 中存在但内存中已移除")
            
            # 如果有差异，更新 DNS
            if needs_update:
                logger.info("ℹ️  检测到内存与 DNS 不一致:")
                for change in changes:
                    logger.info(f"   ├─ {change}")
                logger.info("📝 正在同步内存数据到 DNS...")
                
                # 更新 A 记录（使用 server_port 的公网 IP）
                if 'server_port' in natter_processes:
                    server_public_ip = natter_processes['server_port']['public_ip']
                    update_cloudflare_a_record(server_public_ip)
                
                # 更新 TXT 记录
                if update_cloudflare_txt_record(memory_mapping):
                    logger.info("✅ DNS 记录已同步")
                else:
                    logger.warning("⚠️  DNS 记录同步失败")
            else:
                logger.debug("✅ 内存与 DNS 记录一致，无需更新")
                
        except Exception as e:
            logger.error(f"❌ 检查内存与 DNS 一致性失败: {e}", exc_info=True)
    
    return failed_ports


def validate_cloudflare_config():
    """验证 Cloudflare 配置是否完整"""
    if not DOMAIN:
        logger.error("❌ 未配置 STUN_DOMAIN 环境变量")
        return False
    
    if not CLOUDFLARE_API_TOKEN:
        logger.warning("⚠️  未配置 CLOUDFLARE_API_TOKEN，将无法更新 DNS 记录")
        return True  # 允许继续运行，只是无法更新DNS
    
    return True


def validate_natter_executable():
    """验证 natter 是否存在且可访问"""
    if not os.path.exists(NATTER_PATH):
        logger.error(f"❌ Natter 不存在: {NATTER_PATH}")
        return False
    
    return True


def validate_frps_executable():
    """验证 frps 可执行文件是否存在"""
    if not os.path.exists(FRPS_EXE_PATH):
        logger.error(f"❌ frps 可执行文件不存在: {FRPS_EXE_PATH}")
        return False
    
    if not os.path.exists(FRPS_CONFIG_PATH):
        logger.error(f"❌ frps 配置文件不存在: {FRPS_CONFIG_PATH}")
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
        logger.info("🧹 清理所有 natter 进程...")
        ports_to_clean = list(natter_processes.keys())
    else:
        # 只清理指定的进程
        logger.info(f"🧹 清理指定的 natter 进程: {', '.join(port_names)}")
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
                logger.debug(f"✅ {port_name} 的 natter 进程已退出")
            
            # 从字典中移除
            del natter_processes[port_name]
        except Exception as e:
            logger.warning(f"⚠️  清理 {port_name} 的 natter 进程失败: {e}")
    
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
    
    logger.info(f"🔧 准备重启 {port_name} 的 natter 进程...")
    
    # 1. 获取原来的配置
    if port_name in natter_processes:
        old_local_port = natter_processes[port_name]['local_port']
    else:
        # 从配置文件重新读取
        port_config = read_stun_port_config()
        if port_name not in port_config:
            logger.error(f"❌ 配置中未找到 {port_name}")
            return False
        old_local_port = port_config[port_name]
    
    # 2. 清理旧进程
    cleanup_natter_processes([port_name])
    
    # 3. 重新打洞
    public_ip, public_port, actual_local_port, process = run_natter_for_port(port_name, old_local_port)
    
    if not (public_port and process):
        logger.error(f"❌ {port_name} 重启失败")
        return False
    
    # 4. 更新全局状态
    natter_processes[port_name] = {
        'process': process,
        'public_ip': public_ip,
        'public_port': public_port,
        'local_port': actual_local_port
    }
    
    # 5. 如果是 server_port，需要检查端口是否变化
    if port_name == 'server_port' and actual_local_port != old_local_port:
        logger.warning(f"⚠️  server_port 的本地端口发生变化 ({old_local_port} -> {actual_local_port})")
        logger.info("⚙️  需要重启 frps 以应用新端口配置...")
        
        # 更新配置
        if not update_frps_config(actual_local_port):
            logger.error("❌ 更新 frps 配置失败")
            return False
        
        # 重启 frps
        if not restart_frps():
            logger.error("❌ 重启 frps 失败")
            return False
    
    # 6. 更新 Cloudflare DNS
    # 构造新的端口映射
    port_mapping = {
        pname: {
            'local': info['local_port'],
            'public': info['public_port']
        }
        for pname, info in natter_processes.items()
    }
    
    if port_mapping:
        # 更新 A 记录（使用 server_port 的公网 IP）
        if 'server_port' in natter_processes:
            update_cloudflare_a_record(natter_processes['server_port']['public_ip'])
        
        # 更新 TXT 记录
        update_cloudflare_txt_record(port_mapping)
    
    logger.info(f"✅ {port_name} 重启成功")
    return True


def main():
    """主循环"""
    logger.info("")
    logger.info("="*70)
    logger.info("🌟 Stun_Frps 服务启动")
    logger.info("="*70)
    logger.info(f"📁 配置文件: {STUN_PORT_CONFIG}")
    logger.info(f"🔧 Natter路径: {NATTER_PATH}")
    logger.info(f"🔧 frps路径: {FRPS_EXE_PATH}")
    logger.info(f"🌐 域名: {DOMAIN}")
    logger.info(f"⏱️ 检查间隔: {CHECK_INTERVAL} 秒")
    logger.info(f"🔄 监听模式: 实时更新内存 → 定期同步到 DNS")
    logger.info("-"*70)
    
    # 启动前验证
    logger.info("🔍 验证配置和文件...")
    if not validate_natter_executable():
        logger.error("❌ Natter 验证失败，程序退出")
        sys.exit(1)
    
    if not validate_frps_executable():
        logger.error("❌ frps 验证失败，程序退出")
        sys.exit(1)
    
    if not validate_cloudflare_config():
        logger.error("❌ Cloudflare 配置验证失败，程序退出")
        sys.exit(1)
    
    logger.info("✅ 所有验证通过")
    logger.info("")
    
    # 初始执行一次
    if not perform_stun_and_update():
        logger.error("❌ 初始打洞失败，程序退出")
        sys.exit(1)
    
    # 定期检查
    while True:
        try:
            time.sleep(CHECK_INTERVAL)
            logger.info("🔄 定期检查 natter 进程状态...")
            
            # 检查 natter 进程是否正常运行，并对比内存与 DNS 记录
            failed_ports = check_natter_processes()
            
            if failed_ports:
                logger.warning(f"⚠️ 检测到 {len(failed_ports)} 个端口异常: {', '.join(failed_ports)}")
                logger.info(" 逐个重启异常端口，不影响正常运行的端口...")
                
                # 逐个重启失败的端口
                success_count = 0
                for port_name in failed_ports:
                    if restart_single_natter(port_name):
                        success_count += 1
                    else:
                        logger.warning(f"⚠️  {port_name} 重启失败，将在下次检查时继续尝试")
                
                logger.info(f"✅ 成功重启 {success_count}/{len(failed_ports)} 个端口")
            else:
                logger.info("✅ 所有 natter 进程运行正常")
                
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"❌ 主循环异常: {e}", exc_info=True)
            # 发生异常时也清理一下进程
            cleanup_natter_processes()
            logger.info("⏱️  等待下次检查...")
            time.sleep(60)
    
    # 清理资源
    try:
        logger.info("")
        logger.info("⚠️  接收到退出信号，正在清理...")
        logger.info("🧹 清理资源...")
        cleanup_natter_processes()
        
        if frps_process and frps_process.poll() is None:
            logger.info("🛑 停止 frps 进程...")
            safe_terminate_process(frps_process, "frps", timeout_terminate=5, timeout_kill=2)
        
        logger.info("")
        logger.info("="*70)
        logger.info("👋 服务已停止")
        logger.info("="*70)
        logger.info("")
    except:
        # 清理过程中忽略所有异常，确保能正常退出
        pass


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        # 最外层拦截，确保完成清理流程
        pass
