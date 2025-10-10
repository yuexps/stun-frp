import dns.resolver
import toml
import time
import platform
import subprocess
import re
import os
import sys
import logging
from logging.handlers import RotatingFileHandler

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
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()  # 日志级别

# 获取脚本所在目录
if getattr(sys, 'frozen', False):
    # 如果是打包后的可执行文件
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    # 如果是源码运行
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 日志文件路径（如果是相对路径，则保存在脚本目录下）
LOG_FILE = os.getenv('LOG_FILE', 'stun_frpc.log')
if not os.path.isabs(LOG_FILE):
    LOG_FILE = os.path.join(SCRIPT_DIR, LOG_FILE)

# frpc可执行文件和配置文件路径（根据操作系统自动选择）
FRPC_EXE_PATH = ''
FRPC_CONFIG_PATH = ''

# 存储每个客户端的进程和端口信息
frpc_processes = {}  # {client_number: process}
frpc_connect_ports = {}  # {client_number: public_port}


def setup_logger():
    """配置日志系统"""
    logger = logging.getLogger('Stun_Frpc')
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
    
    # 文件输出
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
            logger.warning(f"⚠️ 无法创建日志文件 {LOG_FILE}: {e}")
    
    return logger


# 初始化日志
logger = setup_logger()

def parse_txt_record(domain, max_retries=3, retry_delay=2):
    """
    解析 DNS TXT 记录，返回所有客户端的配置
    
    Args:
        domain: 域名
        max_retries: 最大重试次数
        retry_delay: 重试延迟（秒）
    
    Returns:
        dict: {client_number: (server_port, remote_port, public_port)}
    """
    for retry in range(max_retries):
        try:
            if retry > 0:
                logger.info(f"DNS 重试 {retry}/{max_retries-1}...")
                time.sleep(retry_delay)
            
            resolver = dns.resolver.Resolver()
            resolver.cache = None  # 禁用缓存
            resolver.nameservers = ['1.1.1.1', '8.8.8.8']
            resolver.timeout = 5  # 5秒超时
            resolver.lifetime = 10  # 总生存时间10秒
            
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
                            logger.debug(f"客户端{client_num}: server_port={server_port}, {local_port_key}={remote_port}, {public_port_key}={public_port}")
            
            # 检查是否所有请求的客户端都找到了配置
            missing_clients = [num for num in CLIENT_NUMBERS if num not in configs]
            if missing_clients:
                logger.warning(f"⚠️ 未找到客户端 {', '.join(map(str, missing_clients))} 的配置")
            
            if configs:
                logger.info(f"✅ 成功解析 {len(configs)}/{len(CLIENT_NUMBERS)} 个客户端配置")
                return configs
            else:
                logger.warning(f"⚠️ 未解析到任何客户端配置，将重试...")
                
        except dns.resolver.NXDOMAIN:
            logger.error(f"域名 {domain} 不存在")
            return {}
        except dns.resolver.NoAnswer:
            logger.error(f"域名 {domain} 没有 TXT 记录")
            return {}
        except dns.resolver.Timeout:
            logger.warning(f"⚠️ DNS 查询超时 (尝试 {retry+1}/{max_retries})")
        except Exception as e:
            logger.warning(f"❌ DNS 查询失败 (尝试 {retry+1}/{max_retries}): {e}")
            if retry == max_retries - 1:
                logger.error("DNS 查询异常详情:", exc_info=True)
    
    logger.error(f"❌ DNS 查询失败，已重试 {max_retries} 次")
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
                logger.info(f"📝 为客户端{client_number}创建配置文件: {config_path}")
        
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
                logger.info(f"⚙️  客户端{client_number} auth.token 已更新")

        # 获取当前代理配置
        old_remote_port = None
        local_ip = "0.0.0.0"
        local_port = None
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
                logger.info(f"⚙️  客户端{client_number}代理名称更新为: {proxy['name']}")
            
            old_remote_port = proxy.get('remotePort')
            # 获取 localIP 和 localPort
            local_ip = proxy.get('localIP', '0.0.0.0')
            local_port = proxy.get('localPort')
            
            # 更新代理远程下发端口（对应 client_local_port）
            if old_remote_port != remote_port:
                proxy['remotePort'] = remote_port
                changed = True
        
        # 更新公网连接端口（对应 client_public_port）
        if frpc_connect_ports.get(client_number) != public_port:
            frpc_connect_ports[client_number] = public_port # 记录当前公网端口
            changed = True

        if not changed:
            return False, config_path, local_ip, local_port  # 无变化

        with open(config_path, 'w') as f:
            toml.dump(config, f)

        logger.info(f"📋 客户端{client_number}: serverAddr={DOMAIN}, serverPort={server_port}, remotePort={remote_port}, 公网端口={public_port}")
        return True, config_path, local_ip, local_port
    except Exception as e:
        logger.error(f"❌ 更新客户端{client_number}配置文件失败: {e}")
        return False, None, None, None

def validate_config(config_path):
    """
    验证 frpc 配置文件是否有效
    
    Args:
        config_path: 配置文件路径
    
    Returns:
        bool: 配置是否有效
    """
    try:
        config = toml.load(config_path)
        
        # 检查必需的字段
        required_fields = ['serverAddr', 'serverPort']
        for field in required_fields:
            if field not in config:
                logger.error(f"配置文件缺少必需字段: {field}")
                return False
        
        # 检查代理配置
        if 'proxies' not in config or not config['proxies']:
            logger.error("配置文件缺少代理配置")
            return False
        
        # 检查第一个代理的配置
        proxy = config['proxies'][0]
        proxy_required = ['name', 'type', 'localPort', 'remotePort']
        for field in proxy_required:
            if field not in proxy:
                logger.error(f"代理配置缺少必需字段: {field}")
                return False
        
        return True
    except Exception as e:
        logger.error(f"❌ 配置文件验证失败: {e}")
        return False


def check_process_health(client_number):
    """
    检查 frpc 进程是否健康运行
    
    Args:
        client_number: 客户端编号
    
    Returns:
        bool: 进程是否健康
    """
    if client_number not in frpc_processes:
        return False
    
    process = frpc_processes[client_number]
    
    # 检查进程是否还在运行
    if process.poll() is not None:
        logger.warning(f"⚠️ 客户端{client_number} frpc 进程已退出 (返回码: {process.returncode})")
        return False
    
    return True


def get_frpc_paths():
    """获取 frpc 可执行文件和配置文件路径，支持 PyInstaller 打包"""
    system = platform.system()
    
    # 判断是否为 PyInstaller 打包后的可执行文件
    if getattr(sys, 'frozen', False):
        # 如果是打包后的可执行文件，使用可执行文件所在目录
        base_dir = os.path.dirname(sys.executable)
    else:
        # 如果是源码运行，使用脚本所在目录
        base_dir = os.path.dirname(os.path.abspath(__file__))
    
    if system == 'Windows':
        exe = os.path.join(base_dir, 'Windows', 'frpc.exe')
        conf = os.path.join(base_dir, 'Windows', 'frpc.toml')
    else:
        exe = os.path.join(base_dir, 'Linux', 'frpc')
        conf = os.path.join(base_dir, 'Linux', 'frpc.toml')
    return exe, conf

FRPC_EXE_PATH, FRPC_CONFIG_PATH = get_frpc_paths()


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
            logger.warning(f"⚠️ {process_name} 未响应 terminate，使用 kill 强制结束...")
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


def start_frpc(client_number, config_path):
    """启动指定客户端的 frpc 进程"""
    global frpc_processes
    try:
        # 验证配置文件
        if not validate_config(config_path):
            logger.error(f"❌ 客户端{client_number}配置文件验证失败")
            return False
        
        # Windows 上不使用 shell=True，避免子进程无法终止
        if platform.system() == 'Windows':
            # Windows: 创建新的进程组，便于终止
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            process = subprocess.Popen(
                [FRPC_EXE_PATH, '-c', config_path],
                creationflags=CREATE_NEW_PROCESS_GROUP
            )
        else:
            # Linux/Unix: 直接启动
            process = subprocess.Popen([FRPC_EXE_PATH, '-c', config_path])
        
        frpc_processes[client_number] = process
        logger.info(f"✅ 客户端{client_number} frpc 已启动 (PID: {process.pid})")
        
        # 短暂等待，检查进程是否立即退出
        time.sleep(0.5)
        if process.poll() is not None:
            logger.error(f"❌ 客户端{client_number} frpc 启动后立即退出 (返回码: {process.returncode})")
            del frpc_processes[client_number]
            return False
        
        return True
    except Exception as e:
        logger.error(f"❌ 启动客户端{client_number} frpc 失败: {e}", exc_info=True)
        return False

def restart_frpc(client_number, config_path):
    """重启指定客户端的 frpc 进程"""
    global frpc_processes
    try:
        # 检查进程是否存在且正在运行
        if client_number in frpc_processes:
            process = frpc_processes[client_number]
            if process.poll() is None:
                logger.info(f"🛑 正在终止客户端{client_number} frpc 进程...")
                if not safe_terminate_process(process, f"客户端{client_number} frpc", timeout_terminate=5, timeout_kill=2):
                    logger.warning(f"⚠️ 客户端{client_number} frpc 可能未完全关闭，但仍继续重启流程")
            else:
                logger.info(f"✅ 客户端{client_number} frpc 进程已不在运行")
            
            # 清理进程记录
            del frpc_processes[client_number]
            
            # 等待服务器端完全释放代理连接
            logger.debug("等待服务器释放代理连接...")
            time.sleep(3)
        
        # 启动新进程
        if start_frpc(client_number, config_path):
            logger.info(f"✅ 客户端{client_number} frpc 重启完成")
            return True
        else:
            logger.error(f"❌ 客户端{client_number} frpc 重启失败")
            return False
    except Exception as e:
        logger.error(f"❌ 重启客户端{client_number} frpc 失败: {e}", exc_info=True)
        # 确保清理进程记录
        if client_number in frpc_processes:
            del frpc_processes[client_number]
        return False

def main():
    logger.info("")
    logger.info("="*70)
    logger.info("🌟 Stun_Frpc 服务启动")
    logger.info("="*70)
    logger.info(f"📋 客户端编号: {', '.join(map(str, CLIENT_NUMBERS))}")
    logger.info(f"🌐 域名: {DOMAIN}")
    logger.info(f"⏱️  检查间隔: {CHECK_INTERVAL} 秒")
    logger.info("-"*70)
    
    # 首次启动前先检查并更新配置
    logger.info("")
    logger.info("🔍 首次检查 DNS TXT 记录...")
    configs = parse_txt_record(DOMAIN)
    
    # 为每个客户端初始化配置和启动进程
    for client_num in CLIENT_NUMBERS:
        if client_num in configs:
            server_port, remote_port, public_port = configs[client_num]
            changed, config_path, local_ip, local_port = update_frpc_config(client_num, server_port, remote_port, public_port)
            if config_path:
                logger.info(f"✅ 客户端{client_num}连接地址: {DOMAIN}:{public_port}")
                if local_ip and local_port:
                    logger.info(f"   └─ 目标地址: {local_ip}:{local_port}")
                if not start_frpc(client_num, config_path):
                    logger.warning(f"❌ 客户端{client_num}启动失败，将在下次检查时继续尝试")
        else:
            logger.warning(f"⚠️ 跳过客户端{client_num}的启动，未找到配置")
    
    # 进入监控循环
    while True:
        try:
            time.sleep(CHECK_INTERVAL)
            logger.info("")
            logger.info("🔄 定期检查端口配置...")
            
            # 先检查进程健康状态
            dead_clients = []
            for client_num in CLIENT_NUMBERS:
                if client_num in frpc_processes and not check_process_health(client_num):
                    dead_clients.append(client_num)
                    del frpc_processes[client_num]
            
            if dead_clients:
                logger.warning(f"⚠️ 检测到 {len(dead_clients)} 个客户端进程异常退出: {', '.join(map(str, dead_clients))}")
            
            # 查询最新配置
            configs = parse_txt_record(DOMAIN)
            
            if not configs:
                logger.warning("⚠️ DNS 查询失败，跳过本次检查")
                continue
            
            # 检查每个客户端的配置
            for client_num in CLIENT_NUMBERS:
                if client_num in configs:
                    server_port, remote_port, public_port = configs[client_num]
                    changed, config_path, local_ip, local_port = update_frpc_config(client_num, server_port, remote_port, public_port)
                    
                    # 如果进程已死亡或配置改变，需要重启
                    if client_num in dead_clients or (changed and config_path):
                        if client_num in dead_clients:
                            logger.warning(f"⚠️ 客户端{client_num}进程异常，尝试重启...")
                            if start_frpc(client_num, config_path):
                                logger.info(f"✅ 客户端{client_num}连接地址: {DOMAIN}:{public_port}")
                                if local_ip and local_port:
                                    logger.info(f"   └─ 目标地址: {local_ip}:{local_port}")
                            else:
                                logger.warning(f"❌ 客户端{client_num}重启失败，将在下次检查时继续尝试")
                        else:
                            if restart_frpc(client_num, config_path):
                                logger.info(f"✅ 客户端{client_num}连接地址: {DOMAIN}:{public_port}")
                                if local_ip and local_port:
                                    logger.info(f"   └─ 目标地址: {local_ip}:{local_port}")
                            else:
                                logger.warning(f"❌ 客户端{client_num}重启失败，将在下次检查时继续尝试")
                    elif not changed:
                        logger.info(f"✅ 客户端{client_num}配置未改变，无需重启")
                else:
                    logger.warning(f"⚠️ 客户端{client_num}未能从 TXT 记录中解析端口，保持当前配置")
                    
        except KeyboardInterrupt:
            logger.info("")
            logger.info("⚠️ 接收到退出信号...")
            break
        except Exception as e:
            logger.error(f"主循环异常: {e}", exc_info=True)
            logger.info("⏱️ 等待下次检查...")
            time.sleep(60)
    
    # 清理资源
    logger.info("")
    logger.info("🧹 清理资源...")
    for client_num, process in list(frpc_processes.items()):
        if process and process.poll() is None:
            logger.info(f"🛑 停止客户端{client_num} frpc...")
            safe_terminate_process(process, f"客户端{client_num} frpc", timeout_terminate=5, timeout_kill=2)
    
    logger.info("")
    logger.info("="*70)
    logger.info("👋 服务已停止")
    logger.info("="*70)
    logger.info("")


if __name__ == '__main__':
    main()
