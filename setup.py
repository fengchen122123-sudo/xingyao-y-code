from setuptools import setup, find_packages

setup(
    name='ycode',
    version='0.1.0',
    packages=find_packages(),
    py_modules=['main'],  # <-- 🌟 核心修复：强行把根目录的 main.py 塞进安装包里
    install_requires=[
        'rich',
        'litellm',
        'pytest',
        'prompt_toolkit',
        'pyfiglet'
    ],
    entry_points={
        'console_scripts': [
            'ycode = main:run_cli',
        ],
    },
)