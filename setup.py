from setuptools import setup, find_packages

setup(
    name='ycode',
    version='1.0.27',
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
