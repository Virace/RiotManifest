# RiotManifest
![](https://img.shields.io/badge/python-%3E%3D3.8-blue)

riot提供的manifest文件进行解析下载

- [介绍](#介绍)
- [安装](#安装)
- [使用](#使用)
- [维护者](#维护者)
- [感谢](#感谢)
- [许可证](#许可证)


### 介绍
目前的功能是可以传入URL或本地文件目录，解析manifest文件，下载文件。

大部分代码都来自于[CommunityDragon/CDTB](https://github.com/CommunityDragon/CDTB)项目，感谢他们的工作。

对`PatcherManifest`进行修改使其支持URL manifest文件下载，细化`filter_files`方法，使其支持正则表达式过滤文件。

对`PatcherFile`增加`download_file`方法，使其支持文件下载。并且使用`aiohttp`进行异步下载。默认并发数为50，并发数可以通过实例化`PatcherManifest`时 `concurrency_limit`参数进行设置； 也可以调用`PatcherFile`的`download_file`方法时传入`concurrency_limit`参数进行设置。

### 安装
```shell
pip3 install riotmanifest
```

poetry
```shell
poetry add riotmanifest
```

### 使用
```python
import asyncio
from riotmanifest.manifest import PatcherManifest
async def main():
    bundle_url = 'https://lol.dyn.riotcdn.net/channels/public/bundles/'
    manifest = PatcherManifest(r"https://lol.secure.dyn.riotcdn.net/channels/public/releases/CB3A1B2A17ED9AAB.manifest", bundle_url=bundle_url)
    
    
    files = list(manifest.filter_files(flag='zh_CN', pattern='wad.client'))

    for file in files:
        await file.download_file(path=r"d:\out")



if __name__ == '__main__':
    asyncio.run(main())
```



### 维护者
**Virace**
- github: [Virace](https://github.com/Virace)
- blog: [孤独的未知数](https://x-item.com)

### 感谢
- [@CommunityDragon](https://github.com/CommunityDragon/CDTB), **CDTB**

- 以及**JetBrains**提供开发环境支持
  
  <a href="https://www.jetbrains.com/?from=kratos-pe" target="_blank"><img src="https://cdn.jsdelivr.net/gh/virace/kratos-pe@main/jetbrains.svg"></a>

### 许可证

[GPLv3](LICENSE)