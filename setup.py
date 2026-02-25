from setuptools import setup, find_packages

setup(
    name="xivbot",
    version="0.1.0",
    description="Terminal agent for paper research powered by DeepXiv SDK",
    author="XivBot",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    python_requires=">=3.9",
    install_requires=[
        "click>=8.0",
        "python-dotenv>=1.0",
        "requests>=2.28",
        "rich>=13.0",
        "openai>=1.0",
    ],
    extras_require={
        "feishu": [
            "flask>=3.0",
            "cryptography>=41.0",
        ],
        "all": [
            "flask>=3.0",
            "cryptography>=41.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "xivbot=xivbot.cli:main",
        ],
    },
)
