import setuptools

from channelrelay import __version__

try:
    long_desc = open("README.md").read()
except IOError:
    long_desc = "Failed to read README.md"

setuptools.setup(
    name="channelrelay",
    version=__version__,
    url="https://github.com/<your-username>/channelrelay",

    author="ChannelRelay contributors",
    author_email="",

    description="Web-based Telegram channel video relay: browse and stream channel media in browser without local storage.",
    long_description=long_desc,
    long_description_content_type="text/markdown",

    packages=setuptools.find_packages(),

    install_requires=[
        "aiohttp>=3",
        "telethon>=1.40",
        "yarl>=1",
    ],
    extras_require={
        "fast": ["cryptg>=0.2"],
    },
    python_requires=">=3.9",

    classifiers=[
        "Development Status :: 4 - Beta",
        "License :: OSI Approved :: GNU Affero General Public License v3 or later (AGPLv3+)",
        "Framework :: AsyncIO",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
)