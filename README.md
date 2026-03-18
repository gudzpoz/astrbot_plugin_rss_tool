# astrbot-plugin-rss-tool

为 AstrBot 添加 RSS 技能，让你的 AstrBot 可以自主获取、筛选 Atom/RSS 信息。
建议配合（目前实验性的）[AstrBot 主动型能力](https://docs.astrbot.app/use/proactive-agent.html)
使用。例如，你可以让 Agent “每天早上 8 点获取一次 RSS 并筛选 XX 领域的新闻”。

## 示例用法

> ### 让 AstrBot 自行订阅并安排推送
>
> > 请你帮我订阅 Nature 与 Science 的 RSS，打上 journal 标签，并预约在十分钟后对
> > journal 标签的前十篇文档进行简要的汇总分析。
>
> 🌐 已使用 web\_search\_tavily 工具 1.4s
>
> 🌐 已使用 web\_search\_tavily 工具 1.6s
>
> 我来帮你订阅这两个期刊的 RSS。根据搜索结果和已知的 Nature RSS feed 格式
> (gist.github.com)，我将使用标准的 RSS 源地址：
>
> 🔧 已使用 rss\_tool\_add 工具 592ms
>
> 🔧 已使用 create\_future\_task 工具 63ms
>
> 🔧 已使用 rss\_tool\_list 工具 1.1s
>
> 已完成！以下是操作汇总：
>
> ##### ✅ RSS订阅成功
>
> |期刊|RSS 地址|标签|
> |---|---|---|
> |Nature| https://www.nature.com/nature.rss |journal|
> |Science| https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science |journal|
>
> ### 十分钟后自动推送
>
> ##### 📊 Journal 标签最新文章汇总分析
> ###### 1️⃣ 文章概览（共 10 篇）……
> ###### 2️⃣ 研究领域分类统计……
> ###### 3️⃣ 跨期刊研究趋势观察……
> ###### 4️⃣ 个人简要评论与见解……
> ###### 5️⃣ 值得关注的研究方向……

（上面的聊天记录使用的是 openrouter/hunter-alpha 模型，需要在设定里开启时间感知。）

## License

[Logo](./logo.png) 来自 [Wikipedia](https://en.wikipedia.org/wiki/File:Feed-icon.svg)，原许可证为 GPL 3.0。
