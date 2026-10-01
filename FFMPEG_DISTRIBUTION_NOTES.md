# FFmpeg 分发合规说明（FFMPEG_DISTRIBUTION_NOTES）

## 随包分发的二进制

| 项 | 值 |
|---|---|
| 组件 | FFmpeg |
| 版本 | 7.1 |
| 构建 | essentials_build，来源 **www.gyan.dev**（`ffmpeg-7.1-essentials_build.7z` 中的 `bin/ffmpeg.exe`） |
| SHA256 | `2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3` |
| 大小 | ~94 MB（详见构建时 `bin/ffmpeg.exe` 实际值） |
| License | **GPLv3**（该 build 的 configure 含 `--enable-gpl --enable-version3`） |
| 许可证文本 | `licenses\FFmpeg\COPYING.GPLv3`（取自 gnu.org 官方文本） |

## 对应源码获取方式（GPLv3 §6 source offer）

分发的是**未修改**的官方构建二进制，对应上游源码：

- 官方源码站：https://ffmpeg.org/download.html
- 精确版本：FFmpeg **n7.1** tag，https://github.com/FFmpeg/FFmpeg/tree/n7.1
- 构建脚本（gyan.dev essentials build 的 configure 参数）由 gyan.dev 在其
  发布页公开；我们未对 FFmpeg 做任何修改、未重新编译。

GPLv3 对「Objective Code 形式分发」要求随附或指明对应源码（§6）。本包的
满足方式：**在 THIRD_PARTY_NOTICES.txt 与本文件中写明精确版本号（n7.1）+
官方源码地址**。由于分发的是未经修改的第三方构建，上述地址即对应源码的
权威获取点；若未来 FFmpeg 官方地址失效，需更换为自存档源码快照并附同等
SHA256 校验信息。

## 结论与阻塞项

- ✅ 随附 COPYING.GPLv3 全文 + 可追溯的版本/哈希/来源 —— 静态合规材料已就位。
- ⚠️ **正式公开发布前的待确认项**（不阻塞 RC2 内测，但公网分发前必须确认）：
  1. gyan.dev 二进制再分发的惯例是允许的（其页面明示 builds 可自由使用），
     但建议正式发布前留存该页面条款的存档截图/文本作为依据。
  2. 若未来把 FFmpeg 换成自编译版本（去 GPL 组件转 LGPL），需整体重建
     本文件与 licenses\FFmpeg\。
  3. 项目自身许可证尚未确定（见 LICENSE-TODO.md）——若选择 GPL 兼容许可
     则无额外问题；若选择专有许可，FFmpeg/gallery-dl/mutagen 的 GPL 要求
     仅约束这三个组件本身，不传染整个程序（独立进程/独立可分离资源），
     但该法律判断建议正式发布前复核。
