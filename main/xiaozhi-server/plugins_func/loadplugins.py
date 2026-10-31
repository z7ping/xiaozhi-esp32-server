import importlib
import os
import pkgutil
from config.logger import setup_logging

TAG = __name__

logger = setup_logging()

def auto_import_modules(package_name):
    """
    自动导入指定包内的所有模块。

    Args:
        package_name (str): 包的名称，如 'functions'。
    """
    # 获取包的路径
    package = importlib.import_module(package_name)
    package_path = package.__path__

    # Cloud-only images may omit heavyweight, unused optional providers.
    # Default (empty env var) preserves existing upstream plugin discovery.
    disabled = {
        name.strip()
        for name in os.environ.get("XIAOZHI_DISABLED_PLUGINS", "").split(",")
        if name.strip()
    }
    for _, module_name, _ in pkgutil.iter_modules(package_path):
        if module_name in disabled:
            logger.bind(tag=TAG).info(f"跳过未安装的可选插件: {module_name}")
            continue
        full_module_name = f"{package_name}.{module_name}"
        importlib.import_module(full_module_name)
        #logger.bind(tag=TAG).info(f"模块 '{full_module_name}' 已加载")