import re
from pathlib import Path

from setuptools import setup, find_packages


def _get_version():
    version_file = Path(__file__).parent / "core" / "version.py"
    text = version_file.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError("Unable to find version string in core/version.py")
    return match.group(1)


setup(
    name='ycode',
    version=_get_version(),
    packages=find_packages(exclude=[
        'tests', 'tests.*',
        'build', 'build.*',
        'dist', 'dist.*',
        'mytemp', 'mytemp.*',
        'bench', 'bench.*',
        'frontend', 'frontend.*',
    ]),
    py_modules=['main'],  # 根目录的 main.py 作为入口模块
    install_requires=[
        # 下限为已验证兼容版本，上限锁到下一个大版本，避免上游 breaking change 静默破坏安装
        'rich>=13.0.0,<16',
        'litellm>=1.0.0,<2',
        'prompt_toolkit>=3.0.0,<4',
        'pyfiglet>=0.8.0,<2',
        'ddgs>=9.0.0,<10',
        'mcp>=1.0.0,<2',
        'psutil>=5.9.0,<8',
        'pillow>=10.0.0,<13',
        'pywebview>=6.0.0,<8',
        'pystray>=0.19.5,<1',
        'keyring>=25.0.0,<27; sys_platform != "win32"',
    ],
    entry_points={
        'console_scripts': [
            'ycode = main:run_cli',
        ]
    },
    extras_require={
        'dev': [
        'pytest>=7.0.0',
        'pytest-mock>=3.0.0',
        ],
    },
)
