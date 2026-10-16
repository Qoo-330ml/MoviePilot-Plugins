# Lux 302代理

此 fork 只保留 MoviePilot V2 的 EmbyReverseProxy 插件，并加入 Lux 网页兼容模式。

Lux 模式默认关闭。开启后，代理会保留请求路径大小写、转发 Lux 网页与 SSE，并保留上游的多个 `Set-Cookie` 响应头。Emby 专用播放和播放器设置在 Lux 模式下不生效。

插件 ID 和源码目录仍为 `EmbyReverseProxy` / `plugins.v2/embyreverseproxy`，以保持原有插件身份和配置关联。

上游项目：[DDSRem-Dev/MoviePilot-Plugins](https://github.com/DDSRem-Dev/MoviePilot-Plugins)

## 许可证

GNU General Public License v3.0，详见 [LICENSE](LICENSE)。
