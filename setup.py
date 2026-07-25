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
    packages=find_packages(),
    py_modules=['main'],  # 根目录的 main.py 作为入口模块
    install_requires=[
        'rich>=13.0.0',
        'litellm>=1.0.0',
        'prompt_toolkit>=3.0.0',
        'pyfiglet>=0.8.0',
        'ddgs>=9.0.0',
        'mcp>=1.0.0',
        'psutil>=5.9.0',
        'pillow>=10.0.0',
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
