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

### 使用
- **异步多并发下载(不推荐)**
```python
import asyncio
from riotmanifest import PatcherManifest
async def main():
    bundle_url = 'https://lol.dyn.riotcdn.net/channels/public/bundles/'
    manifest = PatcherManifest(
      r"https://lol.secure.dyn.riotcdn.net/channels/public/releases/CB3A1B2A17ED9AAB.manifest",
      path=r'E:\out',
      bundle_url=bundle_url)
    
    
    files = list(manifest.filter_files(flag='zh_CN', pattern='wad.client'))

    await manifest.download_files_concurrently(files, 5)



if __name__ == '__main__':
    asyncio.run(main())
```

注意，单个文件的下载并发是50，`download_files_concurrently`方法是对多个文件进行并发下载。建议这个数不要超过10，否则有封IP的风险(实测PatcherManifest传入100，download_files_concurrently传入10，后台可查最大线程为800+，正常执行，量力而行)。
![](https://s2.loli.net/2024/03/16/PUzxQq4sgmp5h2c.png)


- WADExtractor

```python
from riotmanifest.extractor import WADExtractor

we = WADExtractor("DE515F568F4D9C73.manifest")
data = we.extract_files(
    {
        "DATA/FINAL/Champions/Aatrox.wad.client": [
            "data/characters/aatrox/skins/skin0.bin",
            "data/characters/aatrox/skins/skin1.bin",
            "data/characters/aatrox/skins/skin2.bin",
            "data/characters/aatrox/skins/skin3.bin",
        ],
        "DATA/FINAL/Champions/Ahri.wad.client": [
            "data/characters/Ahri/skins/skin0.bin",
            "data/characters/Ahri/skins/skin1.bin",
            "data/characters/Ahri/skins/skin2.bin",
            "data/characters/Ahri/skins/skin3.bin",
        ]
    }
)
print(len(data))
```
该方法无需下载完整WAD文件，直接从manifest中计算需要解包的文件位置，直接获取，减少网络请求。


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
